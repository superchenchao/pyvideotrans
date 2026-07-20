from videotrans.task.trans_create import _nvenc_quality_args


def test_nvenc_medium_uses_quality_first_settings():
    args = _nvenc_quality_args(23, "medium")

    assert args[args.index("-preset") + 1] == "p7"
    assert args[args.index("-tune") + 1] == "hq"
    assert args[args.index("-cq") + 1] == "18"
    assert args[args.index("-multipass") + 1] == "fullres"
    assert "-spatial-aq" in args
    assert "-temporal-aq" in args


def test_nvenc_quality_value_is_clamped():
    low = _nvenc_quality_args(-100, "slow")
    high = _nvenc_quality_args(100, "slow")

    assert low[low.index("-cq") + 1] == "0"
    assert high[high.index("-cq") + 1] == "51"


def test_nvenc_invalid_crf_uses_safe_default():
    args = _nvenc_quality_args("invalid", "medium")

    assert args[args.index("-cq") + 1] == "18"
