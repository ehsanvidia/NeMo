from typing import Callable, Optional

import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks.callback import Callback

from nemo import lightning as nl
from nemo.collections.llm.api import finetune, pretrain
from nemo.collections.llm.gpt.data.mock import MockDataModule
from nemo.collections.llm.gpt.data.squad import SquadDataModule
from nemo.collections.llm.gpt.model.mixtral import MixtralConfig8x7B, MixtralModel
from nemo.collections.llm.peft.lora import LoRA
from nemo.collections.llm.recipes.log.default import default_log, default_resume, tensorboard_logger
from nemo.collections.llm.recipes.optim.adam import distributed_fused_adam_with_cosine_annealing
from nemo.collections.llm.recipes.precision.mixed_precision import bf16_mixed_plugin
from nemo.collections.llm.utils import Config, Partial
from nemo.lightning.pytorch.callbacks.megatron_comm_overlap import MegatronCommOverlapCallback
from nemo.lightning.pytorch.callbacks.moe_token_drop import MegatronTokenDropCallback
from nemo.utils.exp_manager import TimingCallback

NAME = "mixtral_8x7b"


def model() -> Config[pl.LightningModule]:
    return Config(MixtralModel, config=Config(MixtralConfig8x7B))


def trainer(
    tensor_parallelism: int = 1,
    pipeline_parallelism: int = 4,
    pipeline_parallelism_type: Optional[torch.dtype] = torch.bfloat16,
    virtual_pipeline_parallelism: Optional[int] = 8,
    context_parallelism: int = 1,
    sequence_parallelism: bool = False,
    expert_parallelism: int = 8,
    num_nodes: int = 8,
    num_gpus_per_node: int = 8,
    max_steps: int = 1168251,
    callbacks: Optional[list[Config[Callback]]] = None,
) -> Config[nl.Trainer]:
    strategy = Config(
        nl.MegatronStrategy,
        tensor_model_parallel_size=tensor_parallelism,
        pipeline_model_parallel_size=pipeline_parallelism,
        pipeline_dtype=pipeline_parallelism_type,
        virtual_pipeline_model_parallel_size=virtual_pipeline_parallelism,
        context_parallel_size=context_parallelism,
        sequence_parallel=sequence_parallelism,
        expert_model_parallel_size=expert_parallelism,
        gradient_as_bucket_view=True,
        ckpt_async_save=True,
        ckpt_parallel_load=True,
        ddp=Config(
            DistributedDataParallelConfig,
            check_for_nan_in_grad=True,
            grad_reduce_in_fp32=True,
            overlap_grad_reduce=True,
            overlap_param_gather=True,
        ),
    )

    trainer = Config(
        nl.Trainer,
        accelerator="gpu",
        accumulate_grad_batches=1,
        callbacks=callbacks,
        devices=num_gpus_per_node,
        limit_test_batches=50,
        limit_val_batches=32,
        log_every_n_steps=10,
        max_steps=max_steps,
        num_nodes=num_nodes,
        plugins=bf16_mixed_plugin(),
        strategy=strategy,
        use_distributed_sampler=False,
        val_check_interval=2000,
    )

    return trainer


def pretrain_recipe(
    name: str, ckpt_dir: str, num_nodes: int, num_gpus_per_node: int, fn: Callable = pretrain
) -> Partial:
    """
    Create a pre-training recipe for Mixtral 8x7B model.

    This function sets up a complete configuration for pre-training, including
    model, trainer, data, logging, optimization, and resumption settings.

    Args:
        dir (Optional[str]): Directory for saving logs and checkpoints.
        name (str): Name of the pre-training run.
        num_nodes (int): Number of compute nodes to use.
        num_gpus_per_node (int): Number of GPUs per node.
        fn (Callable): The pre-training function to use.

    Returns:
        Partial: Partial configuration for pre-training.

    Examples:
        CLI usage:
            $ nemo llm pretrain --factory mixtral_8x7b
            $ nemo llm pretrain --factory "mixtral_8x7b(num_nodes=8, name='my_mixtral_pretrain')"

        Python API usage:
            >>> recipe = pretrain_recipe(name="mixtral_8x7b_pretrain", num_nodes=8)
            >>> print(recipe)
    """
    return Partial(
        fn,
        model=model(),
        trainer=trainer(
            tensor_parallelism=8,
            pipeline_parallelism=2,
            pipeline_parallelism_type=torch.bfloat16,
            virtual_pipeline_parallelism=None,
            context_parallelism=1,
            sequence_parallelism=True,
            expert_parallelism=1,
            num_nodes=num_nodes,
            num_gpus_per_node=num_gpus_per_node,
            callbacks=[Config(TimingCallback)],
        ),
        data=Config(MockDataModule, seq_length=8192, global_batch_size=512, micro_batch_size=1),
        log=default_log(ckpt_dir=ckpt_dir, name=name, tensorboard_logger=tensorboard_logger(name=name)),
        optim=distributed_fused_adam_with_cosine_annealing(max_lr=3e-4),
        resume=default_resume(),
    )


def pretrain_recipe_performance(
    dir: Optional[str] = None, name: str = "default", num_nodes: int = 8, num_gpus_per_node: int = 8, fn=pretrain
) -> Partial:
    """
    Create a performance-optimized pre-training recipe for Mixtral 8x7B model.

    This recipe enables performance optimizations that may not be suitable for all use cases.
    It builds upon the standard pre-training recipe and adds additional performance enhancements.

    Args:
        dir (Optional[str]): Directory for saving logs and checkpoints.
        name (str): Name of the pre-training run.
        num_nodes (int): Number of compute nodes to use.
        num_gpus_per_node (int): Number of GPUs per node.
        fn (Callable): The pre-training function to use.

    Returns:
        Partial: Partial configuration for performance-optimized pre-training.

    Examples:
        CLI usage:
            $ nemo llm pretrain --factory "mixtral_8x3b.pretrain_recipe_performance(num_nodes=8, name='perf_pretrain')"

        Python API usage:
            >>> recipe = pretrain_recipe_performance(name="mixtral_8x3b_perf", num_nodes=8)
            >>> print(recipe)

    Note:
        Use this recipe with caution and only when you need maximum performance.
        It may not be suitable for all hardware configurations or use cases.
    """
    recipe = pretrain_recipe(name=name, dir=dir, num_nodes=num_nodes, num_gpus_per_node=num_gpus_per_node, fn=fn)
    recipe.trainer.callbacks.extend(
        [
            Config(MegatronTokenDropCallback),
            Config(MegatronCommOverlapCallback),
        ]
    )

    return recipe


def hf_resume() -> Config[nl.AutoResume]:
    """
    Configure automatic resumption from a Hugging Face checkpoint for Mixtral 8x7B model.

    This function sets up the configuration to resume training from a pre-trained
    Hugging Face model checkpoint.

    More info about the model can be found at: https://huggingface.co/mistralai/Mixtral-8x7B-v0.1

    Returns:
        Config[nl.AutoResume]: Configuration for resuming from HuggingFace checkpoint.

    Note:
        This is particularly useful for fine-tuning scenarios where you want to
        start from the pre-trained Mixtral 8x7B model.
    """
    return Config(
        nl.AutoResume,
        restore_config=Config(nl.RestoreConfig, path="hf://mistralai/Mixtral-8x7B-v0.1"),
    )


def finetune_recipe(name: str, ckpt_dir: str, num_nodes: int, num_gpus_per_node: int) -> Partial:
    recipe = pretrain_recipe(
        name=name, ckpt_dir=ckpt_dir, num_nodes=num_nodes, num_gpus_per_node=num_gpus_per_node, fn=finetune
    )
    recipe.resume = hf_resume()
    recipe.peft = Config(LoRA, target_modules=['linear_qkv', 'linear_proj'], dim=32)
    recipe.data = Config(SquadDataModule, seq_length=8192, global_batch_size=512, micro_batch_size=1)
    return recipe
