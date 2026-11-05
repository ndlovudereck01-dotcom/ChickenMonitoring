"""Forms: camera profiles, zones and video uploads."""

from __future__ import annotations

import json

from django import forms
from django.conf import settings
from django.core.validators import FileExtensionValidator

from .ml.media import ALLOWED_VIDEO_EXTENSIONS
from .models import Camera, Zone
from .validators import validate_line, validate_polygon


def _parse_geometry(raw: str, validator, label: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise forms.ValidationError(
            f"{label} must be valid JSON, e.g. [[0.10, 0.20], [0.90, 0.20], [0.90, 0.90]]."
        )
    try:
        validator(value)
    except forms.ValidationError as exc:  # pragma: no cover - passthrough
        raise forms.ValidationError(exc.messages)
    return value


class CameraForm(forms.ModelForm):
    roi = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "input mono", "spellcheck": "false"}),
        help_text="Optional polygon in normalised coordinates (0-1). Leave empty to use the whole frame.",
    )
    counting_line = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "class": "input mono", "spellcheck": "false"}),
        help_text="Two points [[x1,y1],[x2,y2]]; crossings are counted as IN/OUT.",
    )

    class Meta:
        model = Camera
        fields = [
            "name",
            "location",
            "stream_url",
            "is_active",
            "confidence",
            "iou",
            "imgsz",
            "frame_skip",
            "min_track_hits",
            "occupancy_alert_threshold",
            "roi",
            "roi_only",
            "counting_line",
            "invert_line",
            "notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input", "autocomplete": "off"}),
            "location": forms.TextInput(attrs={"class": "input"}),
            "stream_url": forms.TextInput(
                attrs={"class": "input mono", "placeholder": "rtsp://user:pass@192.168.1.20:554/stream1"}
            ),
            "notes": forms.Textarea(attrs={"rows": 3, "class": "input"}),
            "confidence": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": "0.05", "max": "0.95"}),
            "iou": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": "0.1", "max": "0.95"}),
            "imgsz": forms.NumberInput(attrs={"class": "input", "step": "32", "min": "256", "max": "1280"}),
            "frame_skip": forms.NumberInput(attrs={"class": "input", "min": "1", "max": "30"}),
            "min_track_hits": forms.NumberInput(attrs={"class": "input", "min": "1", "max": "60"}),
            "occupancy_alert_threshold": forms.NumberInput(attrs={"class": "input", "min": "1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            if self.instance.roi:
                self.fields["roi"].initial = json.dumps(self.instance.roi)
            if self.instance.counting_line:
                self.fields["counting_line"].initial = json.dumps(self.instance.counting_line)

    def clean_roi(self):
        return _parse_geometry(self.cleaned_data.get("roi") or "", validate_polygon, "ROI")

    def clean_counting_line(self):
        return _parse_geometry(
            self.cleaned_data.get("counting_line") or "", validate_line, "Counting line"
        )

    def clean_confidence(self):
        value = self.cleaned_data["confidence"]
        if not 0.05 <= value <= 0.95:
            raise forms.ValidationError("Confidence must be between 0.05 and 0.95.")
        return value

    def clean_iou(self):
        value = self.cleaned_data["iou"]
        if not 0.10 <= value <= 0.95:
            raise forms.ValidationError("IoU must be between 0.10 and 0.95.")
        return value

    def clean_imgsz(self):
        value = self.cleaned_data["imgsz"]
        if not 256 <= value <= 1280:
            raise forms.ValidationError("Inference size must be between 256 and 1280.")
        return value

    def clean_stream_url(self):
        url = (self.cleaned_data.get("stream_url") or "").strip()
        if url and not url.lower().startswith(
            ("rtsp://", "http://", "https://", "rtmp://", "file://")
        ):
            raise forms.ValidationError("Use an rtsp://, http(s):// or rtmp:// stream URL.")
        return url


class VideoUploadForm(forms.Form):
    video = forms.FileField(
        validators=[
            FileExtensionValidator(
                allowed_extensions=[extension.lstrip(".") for extension in ALLOWED_VIDEO_EXTENSIONS]
            )
        ],
        widget=forms.ClearableFileInput(attrs={"accept": "video/mp4,video/quicktime,video/x-matroska,video/webm"}),
    )
    title = forms.CharField(
        required=False,
        max_length=160,
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "e.g. House 2 — morning check"}),
    )
    camera_profile = forms.ModelChoiceField(
        queryset=Camera.objects.none(),
        required=False,
        empty_label="Global defaults",
        help_text="Borrow ROI, counting line and tuning from a camera profile.",
        widget=forms.Select(attrs={"class": "input"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["camera_profile"].queryset = Camera.objects.all()
        self.fields["video"].help_text = (
            f"MP4, MOV, AVI, MKV or WebM up to {settings.CHICKEN_MAX_UPLOAD_MB} MB."
        )

    def clean_video(self):
        upload = self.cleaned_data["video"]
        limit = settings.CHICKEN_MAX_UPLOAD_MB * 1024 * 1024
        if upload.size > limit:
            raise forms.ValidationError(
                f"File is {upload.size / (1024 * 1024):.0f} MB; the limit is "
                f"{settings.CHICKEN_MAX_UPLOAD_MB} MB."
            )
        if upload.size < 1024:
            raise forms.ValidationError("That file is too small to be a video.")
        return upload


class ZoneForm(forms.ModelForm):
    polygon = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3, "class": "input mono", "spellcheck": "false"}),
        help_text="Normalised polygon with at least 3 points.",
    )

    class Meta:
        model = Zone
        fields = ["name", "kind", "polygon"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "input"}),
            "kind": forms.Select(attrs={"class": "input"}),
        }

    def clean_polygon(self):
        value = _parse_geometry(
            self.cleaned_data.get("polygon") or "", validate_polygon, "Zone polygon"
        )
        if not value:
            raise forms.ValidationError("A zone needs a polygon with at least 3 points.")
        return value


class GeometryForm(forms.Form):
    """Validates the JSON posted by the live geometry editor."""

    roi = forms.JSONField(required=False)
    counting_line = forms.JSONField(required=False)
    invert_line = forms.BooleanField(required=False)
    roi_only = forms.BooleanField(required=False)

    def clean_roi(self):
        value = self.cleaned_data.get("roi")
        if value in (None, []):
            return None
        validate_polygon(value)
        return value

    def clean_counting_line(self):
        value = self.cleaned_data.get("counting_line")
        if value in (None, []):
            return None
        validate_line(value)
        return value
