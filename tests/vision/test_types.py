"""The ``describe_images`` option (ruling V-S1)."""

import pytest
from pydantic import ValidationError

from text_extraction.metadata import MAX_IMAGES
from text_extraction.vision.types import DescribeImagesOptions


def test_max_images_default_5_bounds_1_to_20() -> None:
    assert DescribeImagesOptions().max_images == 5
    assert DescribeImagesOptions.model_validate({}).max_images == 5
    assert DescribeImagesOptions(max_images=1).max_images == 1
    assert DescribeImagesOptions(max_images=20).max_images == 20
    assert MAX_IMAGES == 20  # the bound is the most images the metadata collects
    for bad in (0, 21, -1):
        with pytest.raises(ValidationError) as caught:
            DescribeImagesOptions(max_images=bad)
        assert "max_images" in str(caught.value)
    with pytest.raises(ValidationError):
        DescribeImagesOptions.model_validate({"max_images": 2, "detail": "high"})
