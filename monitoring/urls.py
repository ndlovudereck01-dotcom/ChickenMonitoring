"""URL routing for the monitoring app."""

from django.urls import path

from . import views

app_name = "monitoring"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # Cameras
    path("cameras/", views.camera_list, name="cameras"),
    path("cameras/new/", views.camera_create, name="camera_create"),
    path("cameras/<int:pk>/", views.camera_detail, name="camera_detail"),
    path("cameras/<int:pk>/edit/", views.camera_edit, name="camera_edit"),
    path("cameras/<int:pk>/start/", views.camera_start, name="camera_start"),
    path("cameras/<int:pk>/stop/", views.camera_stop, name="camera_stop"),
    path("cameras/<int:pk>/geometry/", views.camera_geometry, name="camera_geometry"),
    path("cameras/<int:pk>/zones/new/", views.zone_create, name="zone_create"),
    path("zones/<int:pk>/delete/", views.zone_delete, name="zone_delete"),
    # Uploads and runs
    path("videos/upload/", views.video_upload, name="video_upload"),
    path("runs/", views.run_list, name="runs"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("runs/<int:pk>/cancel/", views.run_cancel, name="run_cancel"),
    path("runs/<int:pk>/retry/", views.run_retry, name="run_retry"),
    path("runs/<int:pk>/delete/", views.run_delete, name="run_delete"),
    path("runs/<int:pk>/video/", views.run_video, name="run_video"),
    path("runs/<int:pk>/thumbnail/", views.run_thumbnail, name="run_thumbnail"),
    path("runs/<int:pk>/export/", views.run_export, name="run_export"),
    path("runs/<int:pk>/chart/", views.run_chart_data, name="run_chart"),
    # Live streaming
    path("live/<int:pk>/frame.jpg", views.live_frame, name="live_frame"),
    path("live/<int:pk>/stream.mjpg", views.live_stream, name="live_stream"),
    # JSON APIs
    path("api/runs/<int:pk>/progress/", views.api_run_progress, name="api_run_progress"),
    path("api/cameras/<int:pk>/live/", views.api_camera_live, name="api_camera_live"),
    # Alerts, reports, audit
    path("alerts/", views.alert_list, name="alerts"),
    path("alerts/<int:pk>/acknowledge/", views.alert_acknowledge, name="alert_ack"),
    path("reports/", views.reports, name="reports"),
    path("reports/export/", views.reports_export, name="reports_export"),
    path("audit/", views.audit_log, name="audit"),
    # Ops
    path("healthz", views.healthz, name="healthz"),
]
