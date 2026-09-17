from sync_service.label_caption import build_caption, format_items_html, format_items_plain


def test_format_items_html_plain_for_single_count():
    assert format_items_html([{"sku": "RGL02", "count": 1}]) == "RGL02 × 1"


def test_format_items_html_highlights_count_above_one():
    assert format_items_html([{"sku": "RGL02", "count": 2}]) == "🔴 <b>RGL02 × 2</b>"


def test_format_items_html_escapes_html_special_characters_in_sku():
    assert format_items_html([{"sku": "A&B<C>", "count": 1}]) == "A&amp;B&lt;C&gt; × 1"


def test_format_items_html_joins_multiple_lines():
    items = [{"sku": "A", "count": 1}, {"sku": "B", "count": 3}]
    assert format_items_html(items) == "A × 1\n🔴 <b>B × 3</b>"


def test_build_caption_puts_each_field_on_its_own_line():
    caption = build_caption(marketplace="OZON", store_name="ТЦ Ривьера", order_label="Заказ №123", items=[{"sku": "A", "count": 1}])
    assert caption == "OZON\nТЦ Ривьера\nЗаказ №123\nA × 1"


def test_format_items_plain_no_html_and_no_highlighting():
    items = [{"sku": "A", "count": 1}, {"sku": "B", "count": 3}]
    assert format_items_plain(items) == "A × 1\nB × 3"
