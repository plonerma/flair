import copy
import inspect
import logging
import os
from typing import List

from torch.optim.lr_scheduler import OneCycleLR  # type: ignore

from flair.optim import LinearSchedulerWithWarmup
from flair.trainers.plugins.base import TrainerPlugin, TrainingInterrupt
from flair.trainers.plugins.metric_records import MetricRecord
from flair.training_utils import AnnealOnPlateau

log = logging.getLogger("flair")


class SchedulerPlugin(TrainerPlugin):
    """
    Plugin for all schedulers
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.initial_learning_rate: List = None
        self.current_learning_rate: List = None

        self.scheduler = None

        self.anneal_with_prestarts = None
        self.anneal_with_restarts = None

        self.last_epoch_model_state_dict = None
        self.batch_growth_annealing = None

    @TrainerPlugin.hook
    def before_training_setup(self, scheduler, batch_growth_annealing, **kw):
        """
        Checks for impossible parameter combination
        :param scheduler:
        :param batch_growth_annealing:
        :param kw:
        :return:
        """
        if batch_growth_annealing and (isinstance(scheduler, OneCycleLR) or issubclass(scheduler, OneCycleLR)):
            raise ValueError("Batch growth with OneCycle policy is not implemented.")

        if isinstance(scheduler, AnnealOnPlateau) or issubclass(scheduler, AnnealOnPlateau):
            raise ValueError(f"Use the {AnnealOnPlateauSchedulerPlugin.__name__} with the AnnealOnPlateau scheduler.")

    @TrainerPlugin.hook
    def after_optimizer_setup(
        self,
        dataset_size,
        min_learning_rate,
        train_with_dev,
        anneal_against_dev_loss,
        scheduler,
        cycle_momentum,
        warmup_fraction,
        anneal_factor,
        patience,
        initial_extra_patience,
        scheduler_state_dict,
        batch_growth_annealing,
        mini_batch_size,
        max_epochs,
        epoch,
        anneal_with_prestarts,
        anneal_with_restarts,
        **kw,
    ):
        """
        initialize different schedulers, including anneal target for AnnealOnPlateau, batch_growth_annealing, loading schedulers
        :param dataset_size:
        :param min_learning_rate:
        :param train_with_dev:
        :param anneal_against_dev_loss:
        :param scheduler:
        :param cycle_momentum:
        :param warmup_fraction:
        :param anneal_factor:
        :param patience:
        :param initial_extra_patience:
        :param scheduler_state_dict:
        :param batch_growth_annealing:
        :param mini_batch_size:
        :param max_epochs:
        :param epoch:
        :param anneal_with_prestarts:
        :param anneal_with_restarts:
        :param kw:
        :return:
        """
        optimizer = self.trainer.optimizer

        self.initial_learning_rate = [group["lr"] for group in optimizer.param_groups]

        if not isinstance(min_learning_rate, list):
            min_learning_rate = [min_learning_rate] * len(self.initial_learning_rate)

        for i, lr in enumerate(self.initial_learning_rate):
            if lr < min_learning_rate[i]:
                min_learning_rate[i] = lr / 10

        self.min_learning_rate = min_learning_rate
        self.batch_growth_annealing = batch_growth_annealing

        self.scheduler = scheduler

        if inspect.isclass(scheduler):
            if scheduler == OneCycleLR:
                scheduler_kw = dict(
                    max_lr=self.current_learning_rate,
                    steps_per_epoch=dataset_size // mini_batch_size + 1,
                    epochs=max_epochs - epoch,
                    # if we load a checkpoint, we have already trained for epoch
                    pct_start=0.0,
                    cycle_momentum=cycle_momentum,
                )
            elif scheduler == LinearSchedulerWithWarmup:
                steps_per_epoch = (dataset_size + mini_batch_size - 1) / mini_batch_size
                num_train_steps = int(steps_per_epoch * max_epochs)
                num_warmup_steps = int(num_train_steps * warmup_fraction)

                scheduler_kw = dict(
                    num_train_steps=num_train_steps,
                    num_warmup_steps=num_warmup_steps,
                )
            else:
                # minimize training loss if training with dev data, else maximize dev score
                anneal_mode = "min" if train_with_dev or anneal_against_dev_loss else "max"

                scheduler_kw = dict(
                    factor=anneal_factor,
                    patience=patience,
                    initial_extra_patience=initial_extra_patience,
                    mode=anneal_mode,
                    verbose=True,
                )

        self.current_learning_rate = [group["lr"] for group in self.trainer.optimizer.param_groups]

        # if scheduler is passed as a class, instantiate
        if inspect.isclass(scheduler):
            self.scheduler = scheduler(optimizer, **scheduler_kw)
        else:
            self.scheduler = scheduler

        # load existing scheduler state dictionary if it exists
        if scheduler_state_dict:
            self.scheduler.load_state_dict(scheduler_state_dict)

        self.anneal_with_prestarts = anneal_with_prestarts
        self.anneal_with_restarts = anneal_with_restarts

    @TrainerPlugin.hook
    def before_training_loop(self, **kw):
        """
        Store learning rate and set previous_learning_rate
        :param kw:
        :return:
        """
        self.current_learning_rate = [group["lr"] for group in self.trainer.optimizer.param_groups]
        self.previous_learning_rate = self.current_learning_rate

    @TrainerPlugin.hook
    def before_training_epoch(self, **kw):
        """
        load state for anneal_with_restarts / prestarts, batch_growth_annealing, logic for early stopping
        :param kw:
        :return:
        """
        self.current_learning_rate = [group["lr"] for group in self.trainer.optimizer.param_groups]

        base_path = self.trainer.base_path

        if self.anneal_with_prestarts:
            self.last_epoch_model_state_dict = copy.deepcopy(self.model.state_dict())

        lr_changed = any(
            [lr != prev_lr for lr, prev_lr in zip(self.current_learning_rate, self.previous_learning_rate)]
        )

        if lr_changed and self.batch_growth_annealing:
            self.trainer.mini_batch_size *= 2

        # reload last best model if annealing with restarts is enabled
        if (
            (self.anneal_with_restarts or self.anneal_with_prestarts)
            and lr_changed
            and os.path.exists(base_path / "best-model.pt")
        ):
            if self.anneal_with_restarts:
                log.info("resetting to best model")
                self.model.load_state_dict(self.model.load(base_path / "best-model.pt").state_dict())
            if self.anneal_with_prestarts:
                log.info("resetting to pre-best model")
                self.model.load_state_dict(self.model.load(base_path / "pre-best-model.pt").state_dict())

        self.previous_learning_rate = self.current_learning_rate

        all_lrs_too_small = all([lr < min_lr for lr, min_lr in zip(self.current_learning_rate, self.min_learning_rate)])

        # stop training if learning rate becomes too small
        if not isinstance(self.scheduler, (OneCycleLR, LinearSchedulerWithWarmup)) and all_lrs_too_small:
            raise TrainingInterrupt("learning rate too small - quitting training!")

    @TrainerPlugin.hook
    def after_training_batch(self, **kw):
        """
        do the scheduler step if one-cycle or linear decay

        :param kw:
        :return:
        """
        if isinstance(self.scheduler, (OneCycleLR, LinearSchedulerWithWarmup)):
            self.scheduler.step()
            self.store_learning_rate()


class AnnealOnPlateauSchedulerPlugin(TrainerPlugin):
    """
    Plugin for for AnnealOnPlateau scheduler.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.initial_learning_rate: List = None
        self.current_learning_rate: List = None

        self.scheduler = None

        self.anneal_with_prestarts = None
        self.anneal_with_restarts = None

        self.last_epoch_model_state_dict = None
        self.batch_growth_annealing = None

    @TrainerPlugin.hook
    def before_training_setup(self, scheduler, batch_growth_annealing, **kw):
        """
        Checks for impossible parameter combination
        :param scheduler:
        :param batch_growth_annealing:
        :param kw:
        :return:
        """
        if not isinstance(scheduler, AnnealOnPlateau) or issubclass(scheduler, AnnealOnPlateau):
            raise ValueError(
                f"The {self.__class__.__name__} plugin should only be used with the AnnealOnPlateau scheduler."
            )

    @TrainerPlugin.hook
    def after_optimizer_setup(
        self,
        min_learning_rate,
        train_with_dev,
        anneal_against_dev_loss,
        scheduler,
        cycle_momentum,
        warmup_fraction,
        anneal_factor,
        patience,
        initial_extra_patience,
        scheduler_state_dict,
        batch_growth_annealing,
        anneal_with_prestarts,
        anneal_with_restarts,
        **kw,
    ):
        """
        initialize different schedulers, including anneal target for AnnealOnPlateau, batch_growth_annealing, loading schedulers
        :param min_learning_rate:
        :param train_with_dev:
        :param anneal_against_dev_loss:
        :param scheduler:
        :param cycle_momentum:
        :param warmup_fraction:
        :param anneal_factor:
        :param patience:
        :param initial_extra_patience:
        :param scheduler_state_dict:
        :param batch_growth_annealing:
        :param anneal_with_prestarts:
        :param anneal_with_restarts:
        :param kw:
        :return:
        """
        self.initial_learning_rate = [group["lr"] for group in self.trainer.optimizer.param_groups]

        if not isinstance(min_learning_rate, list):
            min_learning_rate = [min_learning_rate] * len(self.initial_learning_rate)

        for i, lr in enumerate(self.initial_learning_rate):
            if lr < min_learning_rate[i]:
                min_learning_rate[i] = lr / 10

        self.min_learning_rate = min_learning_rate
        self.batch_growth_annealing = batch_growth_annealing

        self.current_learning_rate = [group["lr"] for group in self.trainer.optimizer.param_groups]

        # if scheduler is passed as a class, instantiate
        if inspect.isclass(self.scheduler):
            # minimize training loss if training with dev data, else maximize dev score
            anneal_mode = "min" if train_with_dev or anneal_against_dev_loss else "max"

            self.scheduler = self.scheduler(
                self.trainer.optimizer,
                factor=anneal_factor,
                patience=patience,
                initial_extra_patience=initial_extra_patience,
                mode=anneal_mode,
                verbose=True,
            )
        else:
            self.scheduler = scheduler

        # load existing scheduler state dictionary if it exists
        if scheduler_state_dict:
            self.scheduler.load_state_dict(self.scheduler_state_dict)

        self.anneal_with_prestarts = anneal_with_prestarts
        self.anneal_with_restarts = anneal_with_restarts

    @TrainerPlugin.hook
    def before_training_loop(self, **kw):
        """
        Store learning rate and set previous_learning_rate
        :param kw:
        :return:
        """
        self.current_learning_rate = [group["lr"] for group in self.trainer.optimizer.param_groups]
        self.previous_learning_rate = self.current_learning_rate

    @TrainerPlugin.hook
    def before_training_epoch(self, **kw):
        """
        load state for anneal_with_restarts / prestarts, batch_growth_annealing, logic for early stopping
        :param kw:
        :return:
        """
        self.current_learning_rate = [group["lr"] for group in self.trainer.optimizer.param_groups]

        base_path = self.trainer.base_path

        if self.anneal_with_prestarts:
            self.last_epoch_model_state_dict = copy.deepcopy(self.model.state_dict())

        lr_changed = any(
            [lr != prev_lr for lr, prev_lr in zip(self.current_learning_rate, self.previous_learning_rate)]
        )

        if lr_changed and self.batch_growth_annealing:
            self.trainer.mini_batch_size *= 2

        # reload last best model if annealing with restarts is enabled
        if (
            (self.anneal_with_restarts or self.anneal_with_prestarts)
            and lr_changed
            and os.path.exists(base_path / "best-model.pt")
        ):
            if self.anneal_with_restarts:
                log.info("resetting to best model")
                self.model.load_state_dict(self.model.load(base_path / "best-model.pt").state_dict())
            if self.anneal_with_prestarts:
                log.info("resetting to pre-best model")
                self.model.load_state_dict(self.model.load(base_path / "pre-best-model.pt").state_dict())

        self.previous_learning_rate = self.current_learning_rate

        all_lrs_too_small = all([lr < min_lr for lr, min_lr in zip(self.current_learning_rate, self.min_learning_rate)])

        # stop training if learning rate becomes too small
        if all_lrs_too_small:
            raise TrainingInterrupt("learning rate too small - quitting training!")

    @TrainerPlugin.hook
    def after_training_epoch(self, epoch, **kw):
        """
        Logging for bad_epochs
        :param epoch:
        :param kw:
        :return:
        """

        try:
            bad_epochs = self.scheduler.num_bad_epochs

            self.trainer.dispatch(
                "metric_recorded", MetricRecord.scalar(name="bad_epochs", value=bad_epochs, global_step=epoch)
            )
        except AttributeError:
            # dont record anything
            pass

    @TrainerPlugin.hook
    def after_evaluation(self, current_model_is_best, validation_scores, **kw):
        """
        Scheduler step if AnnealOnPlateau
        :param current_model_is_best:
        :param validation_scores:
        :param kw:
        :return:
        """
        if current_model_is_best:
            self.scheduler.step(*validation_scores)
