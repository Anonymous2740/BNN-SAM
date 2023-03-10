from mmcv.runner.hooks.hook import HOOKS,Hook

from mmcv.runner.hooks.optimizer import OptimizerHook,Fp16OptimizerHook

from mmcv.runner.dist_utils import allreduce_grads
import torch

from mmcv.utils import _BatchNorm
# from mmdet.core.optimizer.PCGrad import PCGradpc_backward 
# why the pc_backward is not ultilized?

@HOOKS.register_module()
class MyOptimizerHook(OptimizerHook):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    def after_train_iter(self, runner):
        runner.optimizer.zero_grad() # set a new zero_grad() function
        # with torch.autograd.detect_anomaly():
        runner.optimizer.pc_backward(runner.outputs['loss'])
        # runner.outputs['loss'].backward() # set a new pcgrad_backward() function
        if self.grad_clip is not None:
            grad_norm = self.clip_grads(runner.model.parameters())
            if grad_norm is not None:
                # Add grad norm to the logger
                runner.log_buffer.update({'grad_norm': float(grad_norm)},
                                         runner.outputs['num_samples'])
        runner.optimizer.step()

@HOOKS.register_module()
class MyFp16OptimizerHook(OptimizerHook):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    def after_train_iter(self, runner):
        """Backward optimization steps for Mixed Precision Training. For
        dynamic loss scaling, please refer `loss_scalar.py`

        1. Scale the loss by a scale factor.
        2. Backward the loss to obtain the gradients (fp16).
        3. Copy gradients from the model to the fp32 weight copy.
        4. Scale the gradients back and update the fp32 weight copy.
        5. Copy back the params from fp32 weight copy to the fp16 model.
        """
        # clear grads of last iteration
        runner.model.zero_grad()
        runner.optimizer.zero_grad()
        # scale the loss value
        scaled_loss = runner.outputs['loss'] * self.loss_scaler.loss_scale
        # scaled_loss.backward()
        runner.optimizer.pc_backward(scaled_loss)
        # copy fp16 grads in the model to fp32 params in the optimizer

        fp32_weights = []
        for param_group in runner.optimizer.param_groups:
            fp32_weights += param_group['params']
        self.copy_grads_to_fp32(runner.model, fp32_weights)
        # allreduce grads
        if self.distributed:
            allreduce_grads(fp32_weights, self.coalesce, self.bucket_size_mb)

        has_overflow = self.loss_scaler.has_overflow(fp32_weights)
        # if has overflow, skip this iteration
        if not has_overflow:
            # scale the gradients back
            for param in fp32_weights:
                if param.grad is not None:
                    param.grad.div_(self.loss_scaler.loss_scale)
            if self.grad_clip is not None:
                grad_norm = self.clip_grads(fp32_weights)
                if grad_norm is not None:
                    # Add grad norm to the logger
                    runner.log_buffer.update({'grad_norm': float(grad_norm)},
                                             runner.outputs['num_samples'])
            # update fp32 params
            runner.optimizer.step()
            # copy fp32 params to the fp16 model
            self.copy_params_to_fp16(runner.model, fp32_weights)
        self.loss_scaler.update_scale(has_overflow)
        if has_overflow:
            runner.logger.warning('Check overflow, downscale loss scale '
                                  f'to {self.loss_scaler.cur_scale}')
@HOOKS.register_module()
class GradientCumulativeOptimizerHookForPC(OptimizerHook):
    """Optimizer Hook implements multi-iters gradient cumulating.
    Args:
        cumulative_iters (int, optional): Num of gradient cumulative iters.
            The optimizer will step every `cumulative_iters` iters.
            Defaults to 1.
    Examples:
        >>> # Use cumulative_iters to simulate a large batch size
        >>> # It is helpful when the hardware cannot handle a large batch size.
        >>> loader = DataLoader(data, batch_size=64)
        >>> optim_hook = GradientCumulativeOptimizerHook(cumulative_iters=4)
        >>> # almost equals to
        >>> loader = DataLoader(data, batch_size=256)
        >>> optim_hook = OptimizerHook()
    """

    def __init__(self, cumulative_iters=1, **kwargs):
        super(GradientCumulativeOptimizerHookForPC, self).__init__(**kwargs)

        assert isinstance(cumulative_iters, int) and cumulative_iters > 0, \
            f'cumulative_iters only accepts positive int, but got ' \
            f'{type(cumulative_iters)} instead.'

        self.cumulative_iters = cumulative_iters
        self.divisible_iters = 0
        self.remainder_iters = 0
        self.initialized = False

    def has_batch_norm(self, module):
        if isinstance(module, _BatchNorm):
            return True
        for m in module.children():
            if self.has_batch_norm(m):
                return True
        return False

    def _init(self, runner):
        if runner.iter % self.cumulative_iters != 0:
            runner.logger.warning(
                'Resume iter number is not divisible by cumulative_iters in '
                'GradientCumulativeOptimizerHook, which means the gradient of '
                'some iters is lost and the result may be influenced slightly.'
            )

        if self.has_batch_norm(runner.model) and self.cumulative_iters > 1:
            runner.logger.warning(
                'GradientCumulativeOptimizerHook may slightly decrease '
                'performance if the model has BatchNorm layers.')

        residual_iters = runner.max_iters - runner.iter

        self.divisible_iters = (
            residual_iters // self.cumulative_iters * self.cumulative_iters)
        self.remainder_iters = residual_iters - self.divisible_iters

        self.initialized = True

    def after_train_iter(self, runner):
        if not self.initialized:
            self._init(runner)

        if runner.iter < self.divisible_iters:
            loss_factor = self.cumulative_iters
        else:
            loss_factor = self.remainder_iters
        # loss = runner.outputs['loss']
        # loss = loss / loss_factor

        loss = runner.outputs['loss']
        for i in range(len(loss)):
            loss[i] = loss[i] / loss_factor
        # loss.backward()
        runner.optimizer.pc_backward(loss)

        if (self.every_n_iters(runner, self.cumulative_iters)
                or self.is_last_iter(runner)):

            if self.grad_clip is not None:
                grad_norm = self.clip_grads(runner.model.parameters())
                if grad_norm is not None:
                    # Add grad norm to the logger
                    runner.log_buffer.update({'grad_norm': float(grad_norm)},
                                             runner.outputs['num_samples'])
            runner.optimizer.step()
            runner.optimizer.zero_grad()
    def is_last_iter(self, runner):
        return runner.iter + 1 == runner._max_iters

@HOOKS.register_module()
class GradientCumulativeOptimizerHook(OptimizerHook):
    """Optimizer Hook implements multi-iters gradient cumulating.
    Args:
        cumulative_iters (int, optional): Num of gradient cumulative iters.
            The optimizer will step every `cumulative_iters` iters.
            Defaults to 1.
    Examples:
        >>> # Use cumulative_iters to simulate a large batch size
        >>> # It is helpful when the hardware cannot handle a large batch size.
        >>> loader = DataLoader(data, batch_size=64)
        >>> optim_hook = GradientCumulativeOptimizerHook(cumulative_iters=4)
        >>> # almost equals to
        >>> loader = DataLoader(data, batch_size=256)
        >>> optim_hook = OptimizerHook()
    """

    def __init__(self, cumulative_iters=1, **kwargs):
        super(GradientCumulativeOptimizerHook, self).__init__(**kwargs)

        assert isinstance(cumulative_iters, int) and cumulative_iters > 0, \
            f'cumulative_iters only accepts positive int, but got ' \
            f'{type(cumulative_iters)} instead.'

        self.cumulative_iters = cumulative_iters
        self.divisible_iters = 0
        self.remainder_iters = 0
        self.initialized = False

    def has_batch_norm(self, module):
        if isinstance(module, _BatchNorm):
            return True
        for m in module.children():
            if self.has_batch_norm(m):
                return True
        return False

    def _init(self, runner):
        if runner.iter % self.cumulative_iters != 0:
            runner.logger.warning(
                'Resume iter number is not divisible by cumulative_iters in '
                'GradientCumulativeOptimizerHook, which means the gradient of '
                'some iters is lost and the result may be influenced slightly.'
            )

        if self.has_batch_norm(runner.model) and self.cumulative_iters > 1:
            runner.logger.warning(
                'GradientCumulativeOptimizerHook may slightly decrease '
                'performance if the model has BatchNorm layers.')

        residual_iters = runner.max_iters - runner.iter

        self.divisible_iters = (
            residual_iters // self.cumulative_iters * self.cumulative_iters)
        self.remainder_iters = residual_iters - self.divisible_iters

        self.initialized = True

    def after_train_iter(self, runner):
        if not self.initialized:
            self._init(runner)

        if runner.iter < self.divisible_iters:
            loss_factor = self.cumulative_iters
        else:
            loss_factor = self.remainder_iters
            
        loss = runner.outputs['loss']
        loss = loss / loss_factor
        loss.backward()


        if (self.every_n_iters(runner, self.cumulative_iters)
                or self.is_last_iter(runner)):

            if self.grad_clip is not None:
                grad_norm = self.clip_grads(runner.model.parameters())
                if grad_norm is not None:
                    # Add grad norm to the logger
                    runner.log_buffer.update({'grad_norm': float(grad_norm)},
                                             runner.outputs['num_samples'])
            runner.optimizer.step()
            runner.optimizer.zero_grad()

    def is_last_iter(self, runner):
        return runner.iter + 1 == runner._max_iters