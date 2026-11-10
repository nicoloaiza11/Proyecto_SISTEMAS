from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('redirect/', views.login_redirect_view, name='login_redirect'),
    path('', views.login_redirect_view, name='home'),
]

