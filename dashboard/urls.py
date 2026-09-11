from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(
        template_name="dashboard/login.html", redirect_authenticated_user=True),
        name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.index, name="index"),
    path("soc/", views.soc_dashboard, name="soc"),
    path("reports/", views.reports, name="reports"),
    path("mapping/", views.mapping, name="mapping"),
    path("api/atrisk/", views.atrisk_partial, name="atrisk_partial"),
    path("device-search/", views.device_search, name="device_search"),
    path("device/<str:mac>/", views.device_360, name="device"),
    path("readiness/", views.policy_readiness, name="readiness"),
    path("config/site-mapping/", views.config_sites, name="config_sites"),
    path("config/settings/", views.config_settings, name="config_settings"),
    path("config/users/", views.config_users, name="config_users"),
    path("config/audit/", views.config_audit, name="config_audit"),
    path("config/activity/", views.config_activity, name="config_activity"),
    path("dataset/<slug:key>/", views.dataset_table, name="dataset"),
    path("dataset/<slug:key>.json", views.dataset_json, name="dataset_json"),
    path("dataset/<slug:key>/data", views.dataset_data, name="dataset_data"),
    path("dataset/<slug:key>/export.csv", views.dataset_csv, name="dataset_csv"),
]
