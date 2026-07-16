from types import SimpleNamespace

from scripts.subtitle_ocr_worker import (
    OcrEngine,
    load_ocr_dependencies,
    preferred_ocr_device,
)


class FakeCuda:
    def __init__(self, count=0):
        self.count = count
        self.empty_cache_calls = 0

    def device_count(self):
        return self.count

    def empty_cache(self):
        self.empty_cache_calls += 1


class FakePaddle:
    def __init__(self, *, cuda_enabled, device_count=0):
        self.cuda_enabled = cuda_enabled
        self.cuda = FakeCuda(device_count)
        self.device = SimpleNamespace(cuda=self.cuda)

    def is_compiled_with_cuda(self):
        return self.cuda_enabled


class FakePredictor:
    def __init__(self, result=None, error=None):
        self.result = result or []
        self.error = error

    def predict(self, image):
        if self.error:
            raise self.error
        return iter(self.result)


def test_load_ocr_dependencies_preloads_torch_before_paddle():
    calls = []
    modules = {
        "torch": object(),
        "cv2": object(),
        "paddle": object(),
        "paddleocr": SimpleNamespace(PaddleOCR=object()),
    }

    def import_module(name):
        calls.append(name)
        return modules[name]

    cv2, paddle, paddle_ocr, torch_preloaded = load_ocr_dependencies(import_module)

    assert calls == ["torch", "cv2", "paddle", "paddleocr"]
    assert cv2 is modules["cv2"]
    assert paddle is modules["paddle"]
    assert paddle_ocr is modules["paddleocr"].PaddleOCR
    assert torch_preloaded is True


def test_preferred_ocr_device_keeps_cpu_only_paddle_on_cpu():
    paddle = FakePaddle(cuda_enabled=False)

    assert preferred_ocr_device(paddle) == ("cpu", "Paddle is not CUDA-enabled")


def test_ocr_engine_falls_back_when_gpu_initialization_fails(capsys):
    paddle = FakePaddle(cuda_enabled=True, device_count=1)
    devices = []

    def create_ocr(**kwargs):
        devices.append(kwargs["device"])
        if kwargs["device"] == "gpu:0":
            raise RuntimeError("CUDA architecture is unsupported")
        return FakePredictor()

    engine = OcrEngine(create_ocr, paddle, detector_dir="models")

    assert engine.device == "cpu"
    assert devices == ["gpu:0", "cpu"]
    assert "GPU OCR initialization failed" in capsys.readouterr().out


def test_ocr_engine_retries_same_input_on_cpu_after_gpu_inference_error(capsys):
    paddle = FakePaddle(cuda_enabled=True, device_count=1)
    devices = []
    expected = [object()]

    def create_ocr(**kwargs):
        devices.append(kwargs["device"])
        if kwargs["device"] == "gpu:0":
            return FakePredictor(error=RuntimeError("out of memory"))
        return FakePredictor(result=expected)

    engine = OcrEngine(create_ocr, paddle, detector_dir="models")

    assert engine.predict("same-frame") == expected
    assert engine.device == "cpu"
    assert devices == ["gpu:0", "cpu"]
    assert paddle.cuda.empty_cache_calls == 1
    assert "retrying the current frame" in capsys.readouterr().out
