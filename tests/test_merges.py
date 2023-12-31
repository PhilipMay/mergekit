import tempfile
from typing import Optional

import pytest
from transformers import LlamaConfig, LlamaForCausalLM

from mergekit.common import MergeOptions
from mergekit.config import (
    InputModelDefinition,
    InputSliceDefinition,
    MergeConfiguration,
    OutputSliceDefinition,
)
from mergekit.merge import run_merge


def make_picollama(path: str):
    cfg = LlamaConfig(
        vocab_size=64,
        hidden_size=128,
        intermediate_size=128,
        num_attention_heads=16,
        num_hidden_layers=1,
    )
    model = LlamaForCausalLM(cfg)
    model.save_pretrained(path, safe_serialization=True)
    return str(path)


@pytest.fixture(scope="session")
def model_a(tmp_path_factory):
    return make_picollama(tmp_path_factory.mktemp("model_a"))


@pytest.fixture(scope="session")
def model_b(tmp_path_factory):
    return make_picollama(tmp_path_factory.mktemp("model_b"))


@pytest.fixture(scope="session")
def model_c(tmp_path_factory):
    return make_picollama(tmp_path_factory.mktemp("model_c"))


class TestMerges:
    def test_gpt2_copy(self):
        config = MergeConfiguration(
            merge_method="passthrough",
            models=[InputModelDefinition(model="gpt2")],
            dtype="bfloat16",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            run_merge(config, out_path=tmpdir, options=MergeOptions())

    def test_gpt2_stack(self):
        config = MergeConfiguration(
            merge_method="passthrough",
            slices=[
                OutputSliceDefinition(
                    sources=[InputSliceDefinition(model="gpt2", layer_range=[0, 12])]
                    * 2
                )
            ],
            dtype="bfloat16",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            run_merge(config, out_path=tmpdir, options=MergeOptions())

    def test_linear_merge(self, model_a, model_b):
        config = self.two_model_config(model_a, model_b, merge_method="linear")
        with tempfile.TemporaryDirectory() as tmpdir:
            run_merge(config, out_path=tmpdir, options=MergeOptions())

    def test_slerp_merge(self, model_a, model_b):
        config = self.two_model_config(
            model_a, model_b, merge_method="slerp", base_model=model_a
        )
        config.parameters = {"t": 0.35}
        with tempfile.TemporaryDirectory() as tmpdir:
            run_merge(config, out_path=tmpdir, options=MergeOptions())

    def test_task_arithmetic_merge(self, model_a, model_b, model_c):
        config = self.two_model_config(
            model_a, model_b, merge_method="task_arithmetic", base_model=model_c
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            run_merge(config, out_path=tmpdir, options=MergeOptions())

    def two_model_config(
        self, model_a, model_b, merge_method: str, base_model: Optional[str] = None
    ):
        config = MergeConfiguration(
            merge_method=merge_method,
            base_model=base_model,
            models=[
                InputModelDefinition(
                    model=model_a,
                    parameters={"weight": 0.6},
                ),
                InputModelDefinition(
                    model=model_b,
                    parameters={"weight": 0.4},
                ),
            ],
            dtype="bfloat16",
        )

        return config