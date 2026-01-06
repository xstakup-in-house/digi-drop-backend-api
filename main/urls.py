from django.urls import path 
from .views import *

urlpatterns=[
    path('login', WalletLoginView.as_view(), name="wallet-login"),
    path("digi-passes", PassListEndpoint.as_view(), name="passes-list"),
    path('profile', UserProfileView.as_view(), name="user-profile"),
    path("profile/stats", UserProfileStatsView.as_view(), name="profile-stats"),
    path('update-profile', UpdateProfileEndpoint.as_view(), name="update-profile"),
    path("passes/<uuid:id>", PassDetailEndpoint.as_view(), name="pass-details"),
    # path("verify/payment", VerifyPaymentView.as_view(), name="pass-payment-verification"),
    path('tasks/', TaskListView.as_view(), name="list_tasks"),
    path('tasks/<int:task_id>/start', StartTaskView.as_view(), name="start-task"),
    path('tasks/<int:task_id>/completed', CompleteTaskView.as_view(), name="task-completion"),
    path('leaderboard/', LeaderboardView.as_view(), name='leaderboard'),
]