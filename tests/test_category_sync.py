from sync_service.category_sync import DEFAULT_NOVICLOUD_CATEGORIES, CategorySyncConfig


def test_load_defaults_to_hardcoded_novicloud_categories_when_no_file(tmp_path):
    config = CategorySyncConfig(str(tmp_path / "category-sync.json"))
    selection = config.load()
    assert selection["novicloud"] == list(DEFAULT_NOVICLOUD_CATEGORIES)
    assert selection["shopify"] == []


def test_save_persists_and_dedupes_sorted(tmp_path):
    config = CategorySyncConfig(str(tmp_path / "category-sync.json"))
    saved = config.save({"novicloud": ["B", "A", "A"], "shopify": ["C"]})
    assert saved == {"novicloud": ["A", "B"], "shopify": ["C"]}
    reloaded = CategorySyncConfig(str(tmp_path / "category-sync.json")).load()
    assert reloaded == {"novicloud": ["A", "B"], "shopify": ["C"]}
