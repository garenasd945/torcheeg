from typing import List, Tuple, Union

import numpy as np
import torch
from torch import Tensor, nn
from torch.nn import functional as F
from torch.utils.data import DataLoader

from ..classifier import ClassifierTrainer


class FocalLoss(nn.Module):

    def __init__(self,
                 gamma: float = 1.0,
                 weight: Tensor = None,
                 reduction: str = 'mean'):
        '''
        Focal loss for imbalanced datasets.

        - Paper: Lin T Y, Goyal P, Girshick R, et al. Focal loss for dense object detection[C]//Proceedings of the IEEE international conference on computer vision. 2017: 2980-2988.
        - URL: https://openaccess.thecvf.com/content_ICCV_2017/papers/Lin_Focal_Loss_for_ICCV_2017_paper.pdf
        - Related Project: https://github.com/clcarwin/focal_loss_pytorch

        Args:
            gamma (float): The gamma parameter. (default: :obj:`1.0`)
        '''
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.register_buffer('weight', weight)
        self.reduction = reduction

    def forward(self, input: Tensor, target: Tensor) -> Tensor:
        ce_loss = F.cross_entropy(input, target, reduction='none')
        p = torch.exp(-ce_loss)
        loss = (1 - p)**self.gamma * ce_loss

        if self.reduction == 'mean':
            return loss.mean()
        elif self.reduction == 'sum':
            return loss.sum()
        else:
            return loss
        
class FocalLossTrainer(ClassifierTrainer):
    r'''
        A trainer class for EEG classification with Focal loss for imbalanced datasets.

        - Paper: Lin T Y, Goyal P, Girshick R, et al. Focal loss for dense object detection[C]//Proceedings of the IEEE international conference on computer vision. 2017: 2980-2988.
        - URL: https://openaccess.thecvf.com/content_ICCV_2017/papers/Lin_Focal_Loss_for_ICCV_2017_paper.pdf
        - Related Project: https://github.com/clcarwin/focal_loss_pytorch

        .. code-block:: python

            trainer = FocalLossTrainer(model, num_classes=2, class_frequency=train_loader)
            trainer.fit(train_loader, val_loader)
            trainer.test(test_loader)

            trainer = FocalLossTrainer(model, num_classes=2, class_frequency=[10, 20], gamma=1.0)
            trainer.fit(train_loader, val_loader)
            trainer.test(test_loader)

        Args:
            model (nn.Module): The classification model, and the dimension of its output should be equal to the number of categories in the dataset. The output layer does not need to have a softmax activation function.
            num_classes (int): The number of classes in the dataset.
            class_frequency (List[int] or Dataloader): The frequency of each class in the dataset. It can be a list of integers or a dataloader to calculate the frequency of each class in the dataset, traversing the data batch (:obj:`torch.utils.data.dataloader.DataLoader`, :obj:`torch_geometric.loader.DataLoader`, etc). (default: :obj:`None`)
            gamma (float): The gamma parameter. (default: :obj:`1.0`)
            rule (str): The rule to adjust the weight of each class. Available options are: 'none', 'reweight', 'drw' (deferred re-balancing optimization schedule). (default: :obj:`none`)
            beta_reweight (float): The beta parameter for reweighting. It is only used when :obj:`rule` is 'reweight' or 'drw'. (default: :obj:`0.9999`)
            drw_epochs (int): The number of epochs to use DRW. It is only used when :obj:`rule` is 'drw'. (default: :obj:`160`)
            lr (float): The learning rate. (default: :obj:`0.001`)
            weight_decay (float): The weight decay. (default: :obj:`0.0`)
            devices (int): The number of devices to use. (default: :obj:`1`)
            accelerator (str): The accelerator to use. Available options are: 'cpu', 'gpu'. (default: :obj:`"cpu"`)
    '''

    def __init__(self,
                 model: nn.Module,
                 num_classes: int,
                 class_frequency: Union[List[int], DataLoader],
                 gamma: float = 0.5,
                 rule: str = "reweight",
                 beta_reweight: float = 0.9999,
                 drw_epochs: int = 160,
                 lr: float = 1e-3,
                 weight_decay: float = 0.0,
                 devices: int = 1,
                 accelerator: str = "cpu",
                 metrics: List[str] = ["accuracy"]):
        super().__init__(model, num_classes, lr, weight_decay, devices,
                         accelerator, metrics)
        self.gamma = gamma
        self.class_frequency = class_frequency
        self.rule = rule
        self.beta_reweight = beta_reweight
        self.drw_epochs = drw_epochs

        if isinstance(class_frequency, DataLoader):
            _class_frequency = [0] * self.num_classes
            for _, y in class_frequency:
                assert y < self.num_classes, f"The label in class_frequency ({y}) is out of range 0-{self.num_classes-1}."
                _class_frequency[y] += 1
            self._class_frequency = _class_frequency
        else:
            self._class_frequency = class_frequency

        assert self.rule in ["none", "reweight",
                             "drw"], f"Unsupported rule: {self.rule}."

        if self.rule == "none":
            _weight = None
            self._weight = _weight
        elif self.rule == "reweight":
            effective_num = 1.0 - np.power(self.beta_reweight, self._class_frequency)
            _weight = (1.0 - self.beta_reweight) / np.array(effective_num)
            _weight = _weight / np.sum(_weight) * self.num_classes
            self._weight = torch.tensor(_weight).float()
        else:
            _weight = [1.0] * self.num_classes
            effective_num = 1.0 - np.power(self.beta_reweight, self._class_frequency)
            _drw_weight = (1.0 - self.beta_reweight) / np.array(effective_num)
            _drw_weight = _drw_weight / np.sum(_drw_weight) * self.num_classes
            self._drw_weight = torch.tensor(_drw_weight).float()
            self._weight = torch.tensor(_weight).float()

        self.focal_fn = FocalLoss(gamma=self.gamma, weight=self._weight)

    def on_train_epoch_start(self) -> None:
        # get epoch
        epoch = self.current_epoch
        if epoch == self.drw_epochs and self.rule == "drw":
            # reset the weight buffer in FocalLoss
            self.focal_fn = FocalLoss(gamma=self.gamma, weight=self._drw_weight).to(self.device)
        return super().on_train_epoch_start()

    def training_step(self, batch: Tuple[torch.Tensor],
                      batch_idx: int) -> torch.Tensor:
        x, y = batch
        y_hat = self(x)
        loss = self.focal_fn(y_hat, y)

        # log to prog_bar
        self.log("train_loss",
                 self.train_loss(loss),
                 prog_bar=True,
                 on_epoch=False,
                 logger=False,
                 on_step=True)

        for i, metric_value in enumerate(self.train_metrics.values()):
            self.log(f"train_{self.metrics[i]}",
                     metric_value(y_hat, y),
                     prog_bar=True,
                     on_epoch=False,
                     logger=False,
                     on_step=True)

        return loss

    def validation_step(self, batch: Tuple[torch.Tensor],
                        batch_idx: int) -> torch.Tensor:
        x, y = batch
        y_hat = self(x)
        loss = self.focal_fn(y_hat, y)

        self.val_loss.update(loss)
        self.val_metrics.update(y_hat, y)
        return loss

    def test_step(self, batch: Tuple[torch.Tensor],
                  batch_idx: int) -> torch.Tensor:
        x, y = batch
        y_hat = self(x)
        loss = self.focal_fn(y_hat, y)

        self.test_loss.update(loss)
        self.test_metrics.update(y_hat, y)
        return loss