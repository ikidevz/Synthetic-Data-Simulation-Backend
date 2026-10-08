import os

import pytest

from app.config.entities import load_entity_configs
from app.services.generator import generate_row, generate_field_value


# a tests-owned dir, not the repo configs/
from conftest import TEST_CONFIG_DIR as CONFIG_DIR


def test_loads_valid_configs():
    configs = load_entity_configs(CONFIG_DIR)
    assert "customers" in configs
    assert "orders" in configs
    assert configs["customers"].primary_key_field == "customer_id"
    assert configs["orders"].fields["customer_id"].type == "ref"


def test_rejects_unknown_field_type(tmp_path):
    bad_yaml = """
entity: broken
fields:
  id:
    type: not_a_real_type
    primary_key: true
"""
    (tmp_path / "broken.yaml").write_text(bad_yaml)
    with pytest.raises(ValueError):
        load_entity_configs(str(tmp_path))


def test_rejects_dangling_ref(tmp_path):
    bad_yaml = """
entity: widgets
fields:
  widget_id:
    type: uuid
    primary_key: true
  owner_id:
    type: ref
    ref_entity: nonexistent_entity
"""
    (tmp_path / "widgets.yaml").write_text(bad_yaml)
    with pytest.raises(ValueError):
        load_entity_configs(str(tmp_path))


def test_generate_field_value_respects_ranges():
    from app.config.entities import FieldConfig

    f = FieldConfig(type="float", min=10, max=20)
    for _ in range(50):
        v = generate_field_value(f)
        assert 10 <= v <= 20

    f_enum = FieldConfig(type="enum", values=["a", "b", "c"])
    for _ in range(20):
        assert generate_field_value(f_enum) in {"a", "b", "c"}


def test_generate_row_skips_version_and_resolves_ref():
    configs = load_entity_configs(CONFIG_DIR)
    orders_cfg = configs["orders"]

    row = generate_row(orders_cfg, existing_ids={"customers": ["c-1", "c-2"]})
    assert "version" not in row
    assert row["customer_id"] in {"c-1", "c-2"}
    assert 10 <= row["amount"] <= 500
    assert row["status"] in {"pending", "shipped", "cancelled"}
    assert row["deleted_at"] is None


def test_generate_row_raises_if_ref_target_unseeded():
    configs = load_entity_configs(CONFIG_DIR)
    orders_cfg = configs["orders"]
    with pytest.raises(ValueError):
        generate_row(orders_cfg, existing_ids={})
