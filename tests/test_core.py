from facetmedia.core import hello


def test_hello_default() -> None:
    assert hello() == "Hello from facetmedia, world!"


def test_hello_custom() -> None:
    assert hello("test") == "Hello from facetmedia, test!"
