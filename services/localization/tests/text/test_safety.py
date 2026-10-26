"""Contact details in a line: what is caught, what is not, and what the source
line is allowed to carry."""

import pytest

from localization.text.safety import contacts_missing_from_source, find_contacts


@pytest.mark.parametrize(
    "text",
    [
        "ghé shop-xyz.vn mua nhé",
        "vào https://shop.example.com/abc ngay",
        "xem www.shop-abc.net",
        "liên hệ qua mail abc.def@gmail.com",
        "gọi 0912345678 để đặt hàng",
        "gọi 0912 345 678 để đặt hàng",
        "gọi 091-234-5678 để đặt hàng",
        "gọi +84 912 345 678 ngay",
        "gọi (028) 3822 1234",
        "ghé shop xyz chấm vn nhé",
        "ghé shopxyz dot com",
        # Look-alike characters and invisible ones in the way.
        "ghé shop-xyz．vn nhé",
        "ghé shop-xyz.v​n nhé",
        "ｗｗｗ.shop.com",
    ],
)
def test_contact_details_are_found(text: str) -> None:
    assert find_contacts(text), text


@pytest.mark.parametrize(
    "text",
    [
        "xong rồi.Nhưng mà",  # a missing space after a full stop is not a domain
        "giá 3.5 triệu",
        "giá 1.500.000 đồng",
        "năm 2024 giảm 30%",
        "lúc 12.30 hôm nay",
        "mua 15 ngày",
        "gọi 112 hoặc 113",
        "iPhone 15 Pro Max",
        "",
    ],
)
def test_ordinary_text_is_not_flagged(text: str) -> None:
    assert find_contacts(text) == [], text


def test_the_contacts_come_back_in_order_without_repeats() -> None:
    found = find_contacts("a.com rồi b.vn rồi a.com và 0912345678")
    assert found == ["a.com", "b.vn", "0912345678"]


def test_a_url_is_one_contact_not_a_url_and_a_domain() -> None:
    assert find_contacts("https://shop-xyz.vn/a") == ["https://shop-xyz.vn/a"]


def test_a_contact_the_source_carries_is_allowed() -> None:
    assert contacts_missing_from_source("ghé shop-xyz.vn nhé", "大家去 shop-xyz.vn 看看") == []


def test_a_contact_only_the_translation_has_is_reported() -> None:
    assert contacts_missing_from_source("ghé shop-xyz.vn nhé", "大家去看看") == ["shop-xyz.vn"]


def test_a_phone_number_matches_the_source_however_it_is_spaced() -> None:
    assert contacts_missing_from_source("gọi 0912 345 678", "电话 0912345678") == []
    assert contacts_missing_from_source("gọi 0912 345 678", "电话 0912345679") != []


def test_the_source_spelling_is_compared_without_case_or_spaces() -> None:
    assert contacts_missing_from_source("vào Shop-XYZ.vn", "shop-xyz.VN") == []


def test_a_look_alike_in_the_translation_is_matched_to_its_ascii_source() -> None:
    assert contacts_missing_from_source("vào shop-xyz．vn", "shop-xyz.vn") == []
