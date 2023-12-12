# Copyright (C) 2023 Charles O. Goddard
#
# This software is free software: you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This software is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program. If not, see http://www.gnu.org/licenses/.

import logging
from typing import Optional

import tqdm
import transformers
from pydantic import BaseModel

from mergekit.architecture import get_architecture_info
from mergekit.common import ModelReference, parse_kmb
from mergekit.config import MergeConfiguration
from mergekit.graph import Executor
from mergekit.plan import MergePlanner
from mergekit.tasks import LoaderCache, TokenizerInfo


class MergeOptions(BaseModel):
    allow_crimes: bool = False
    transformers_cache: Optional[str] = None
    lora_merge_cache: Optional[str] = None
    cuda: bool = False
    low_cpu_memory: bool = False
    out_shard_size: int = parse_kmb("5B")
    copy_tokenizer: bool = True
    allow_crimes: bool = False
    clone_tensors: bool = False
    trust_remote_code: bool = False
    random_seed: Optional[int] = None
    lazy_unpickle: bool = False


def run_merge(merge_config: MergeConfiguration, out_path: str, options: MergeOptions):
    if options.random_seed is not None:
        transformers.trainer_utils.set_seed(options.random_seed)

    if not merge_config.models and not merge_config.slices:
        raise RuntimeError("No output requested")

    model_arch_info = [
        get_architecture_info(m.config()) for m in merge_config.referenced_models()
    ]
    if not options.allow_crimes:
        if not all(a == model_arch_info[0] for a in model_arch_info[1:]):
            raise RuntimeError(
                "Must specify --allow-crimes to attempt to mix different architectures"
            )
    arch_info = model_arch_info[0]

    # initialize loader cache and set options
    loader_cache = LoaderCache()
    loader_cache.lazy_unpickle = options.lazy_unpickle
    loader_cache.lora_cache_dir = options.lora_merge_cache
    loader_cache.hf_cache_dir = options.transformers_cache

    targets = MergePlanner(
        merge_config,
        arch_info,
        out_path=out_path,
        max_shard_size=options.out_shard_size,
        clone_tensors=options.clone_tensors,
    ).plan()

    # warm up loader cache
    for model in tqdm.tqdm(
        merge_config.referenced_models(), desc="Warmup loader cache"
    ):
        loader_cache.get(model)

    exec = Executor(
        tasks=targets,
        math_device="cuda" if options.cuda else "cpu",
        storage_device="cuda" if options.low_cpu_memory else "cpu",
    )

    tokenizer = None
    for _task, value in exec.run():
        if isinstance(value, TokenizerInfo):
            tokenizer = value.tokenizer

    cfg_out = _model_out_config(merge_config)
    if tokenizer:
        try:
            cfg_out.vocab_size = len(tokenizer.get_vocab())
        except Exception as e:
            logging.warning(
                "Unable to set vocabulary size in output config - you may need to manually correct it.",
                exc_info=e,
            )

    try:
        num_layers = sum(
            s.sources[0].layer_range[1] - s.sources[0].layer_range[0]
            for s in merge_config.slices
        )
        setattr(cfg_out, arch_info.num_layers_config_key(), num_layers)
    except Exception as e:
        logging.warning(
            "Unable to set number of layers in output config - you may need to manually correct it.",
            exc_info=e,
        )
    cfg_out.save_pretrained(out_path)

    if tokenizer is None and options.copy_tokenizer:
        tokenizer = _get_donor_tokenizer(merge_config)

    if tokenizer:
        tokenizer.save_pretrained(out_path, safe_serialization=True)


def _get_donor_tokenizer(merge_config: MergeConfiguration):
    try:
        donor_model = merge_config.base_model
        if donor_model:
            donor_model = ModelReference.parse(donor_model)
        if not donor_model:
            donor_model = merge_config.referenced_models()[0]

        return transformers.AutoTokenizer.from_pretrained(donor_model.path)
    except Exception as e:
        logging.error(
            "Failed to copy tokenizer. The merge was still successful, just copy it from somewhere else.",
            exc_info=e,
        )
        return None


def _model_out_config(config: MergeConfiguration) -> transformers.PretrainedConfig:
    """Return a configuration for the resulting model."""
    if config.base_model:
        res = ModelReference.parse(config.base_model).config()
    else:
        res = config.referenced_models()[0].config()
    if config.dtype:
        res.torch_dtype = config.dtype
    return res


__all__ = ["MergeOptions", "run_merge"]
