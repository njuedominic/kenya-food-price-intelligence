from kenya_food_intelligence import PLATFORM_NAME, __version__


def test_package_metadata_is_available() -> None:
    assert PLATFORM_NAME == "Kenya Food Price & Inflation Intelligence Platform"
    assert __version__ == "0.1.0"
