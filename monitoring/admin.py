from django.contrib import admin

from .models import Alert, AuditLog, BirdTrack, Camera, CountSample, DetectionRun, VideoAsset, Zone, ZoneStatistic


@admin.register(Camera)
class CameraAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "status", "is_active", "occupancy_alert_threshold", "last_seen_at")
    list_filter = ("status", "is_active")
    search_fields = ("name", "location")
    readonly_fields = ("created_at", "updated_at", "last_seen_at")


@admin.register(DetectionRun)
class DetectionRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "title", "source_type", "status", "unique_birds", "peak_occupancy",
        "created_at", "finished_at",
    )
    list_filter = ("status", "source_type")
    search_fields = ("title",)
    readonly_fields = ("created_at", "started_at", "finished_at", "processing_fps")


@admin.register(VideoAsset)
class VideoAssetAdmin(admin.ModelAdmin):
    list_display = ("original_name", "duration_seconds", "size_bytes", "uploaded_by", "created_at")
    readonly_fields = ("sha256", "size_bytes", "duration_seconds", "fps", "width", "height")


@admin.register(Zone)
class ZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "camera", "kind", "is_active")
    list_filter = ("kind", "is_active")


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "severity", "camera", "message", "is_acknowledged")
    list_filter = ("kind", "severity", "is_acknowledged")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "user", "target_type", "target_id", "ip_address")
    list_filter = ("action",)
    search_fields = ("detail", "target_id")
    readonly_fields = [field.name for field in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(BirdTrack)
admin.site.register(CountSample)
admin.site.register(ZoneStatistic)

admin.site.site_header = "Chicken Vision administration"
