# signals.py
from django.db.models.signals import post_save

from django.dispatch import receiver
from .models import Profile, DigiUser, Task, UserTaskCompletion

@receiver(post_save, sender=DigiUser)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)

@receiver(post_save, sender=DigiUser)
def save_user_profile(sender, instance, **kwargs):
    instance.profile.save()


@receiver(post_save, sender=Profile)
def check_profile_completion(sender, instance, **kwargs):
    # Example: If names and email filled, complete "complete_profile" task
    if instance.names and instance.email:
        task = Task.objects.filter(title="Complete Your Profile").first()
        if task and not UserTaskCompletion.objects.filter(user=instance.user, task=task).exists():
            completion = UserTaskCompletion(user=instance.user, task=task, awarded_points=task.points)
            completion.save()
            instance.scored_point += task.points
            instance.save()