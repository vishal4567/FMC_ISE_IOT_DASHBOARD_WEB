from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.index, name="index"),
    path("reports/", views.reports, name="reports"),
    path("mapping/", views.mapping, name="mapping"),
    path("api/atrisk/", views.atrisk_partial, name="atrisk_partial"),
    path("device-search/", views.device_search, name="device_search"),
    path("device/<str:mac>/", views.device_360, name="device"),
    path("readiness/", views.policy_readiness, name="readiness"),
    path("config/site-mapping/", views.config_sites, name="config_sites"),
    path("dataset/<slug:key>/", views.dataset_table, name="dataset"),
    path("dataset/<slug:key>.json", views.dataset_json, name="dataset_json"),
    path("dataset/<slug:key>/export.csv", views.dataset_csv, name="dataset_csv"),
]
