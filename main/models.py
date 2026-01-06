import uuid
from django.db import models
from django.utils import timezone
from datetime import timedelta
from django.utils.translation import gettext_lazy as _
from .manager import UserManager
from django.contrib.auth.models import AbstractBaseUser

# Create your models here.


class DigiUser(AbstractBaseUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet_address = models.CharField(max_length=42, unique=True)  # e.g., 0xabc...123
    last_connected_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)

    objects = UserManager()

    USERNAME_FIELD = 'wallet_address'  # Auth via wallet_address
    REQUIRED_FIELDS = []  # No other required fields

    def __str__(self):
        return self.wallet_address

    class Meta:
        verbose_name = _("User")
        verbose_name_plural=_("Users")

    def has_perm(self, perm, obj=None):
        return True

    def has_module_perms(self, app_label):
        return True
    
    def __str__(self):
        return self.wallet_address


class LoginNonce(models.Model):
    nonce = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    used = models.BooleanField(default=False)

    def is_expired(self):
        return timezone.now() - self.created_at > timedelta(minutes=5)

    def __str__(self):
        return f"{self.nonce[:8]}... ({'used' if self.used else 'fresh'})"

class DigiPass(models.Model):
    pass_id = models.AutoField(primary_key=True)
    id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    usd_price = models.DecimalField(max_digits=20, decimal_places=2)
    pass_type = models.CharField(max_length=50)
    point_power = models.PositiveIntegerField(default=2)
    card = models.ImageField(upload_to="ntfpass")


    def __str__(self):
        return self.name

class Profile(models.Model):
    user = models.OneToOneField(DigiUser, related_name="profile", on_delete=models.CASCADE)
    names = models.CharField(max_length=100, null=True, blank=True)
    email = models.EmailField(max_length=200, null=True, blank=True)
    scored_point = models.PositiveBigIntegerField(default=0, db_index=True)
    last_login_date = models.DateField(null=True, blank=True)
    has_pass = models.BooleanField(default=False)
    current_pass = models.ForeignKey(DigiPass, null=True, blank=True, on_delete=models.SET_NULL)
    referral_code = models.CharField(max_length=10, unique=True, editable=False)
    referred_by = models.ForeignKey(DigiUser, on_delete=models.CASCADE, related_name="referred_users", blank=True, null=True)

    def save(self, *args, **kwargs):
        if not self.referral_code:
            self.referral_code = uuid.uuid4().hex[:10].upper()  # Unique 10-char code
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.wallet_address}-profile"
    
    class Meta:
        indexes = [models.Index(fields=['-scored_point'])]
    

# models.py
class PassTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey('DigiUser', related_name="pass_transactions", on_delete=models.CASCADE)
    wallet_address = models.CharField(max_length=42, help_text="The user's connected wallet address")
    digipass = models.ForeignKey(DigiPass, on_delete=models.CASCADE)
    tx_hash = models.CharField(max_length=66, unique=True, blank=True, null=True,  db_index=True)
    minted = models.BooleanField(default=False, db_index=True)
    usd_price = models.DecimalField(max_digits=20, decimal_places=2)
    amount_paid_bnb = models.DecimalField(max_digits=18, decimal_places=8, help_text="Amount of BNB paid")
    is_verified = models.BooleanField(default=False, help_text="Whether the transaction has been verified on-chain",  db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_upgrade = models.BooleanField(default=False)

    def __str__(self):
        return f"Trans- {self.id}- {self.digipass.name}"
    
    class Meta:
        indexes = [
            models.Index(fields=["user", "is_verified", "minted", "-created_at"]),
            models.Index(fields=["tx_hash"]),
        ]


class Task(models.Model):

    title = models.CharField(max_length=200)
    description = models.TextField()
    points = models.PositiveIntegerField(default=10)
    icon = models.CharField(max_length=20, null=True, blank=True)
    task_type = models.CharField(max_length=50, choices=[('on_site', 'On-Site'), ('off_site', 'Off-Site')])  # e.g., 'complete_profile', 'follow_instagram'
    external_link = models.URLField(blank=True, null=True)  # For off-site, e.g., Instagram URL
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.title
    
class UserTaskCompletion(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        STARTED = 'started', 'Started'
        COMPLETED = 'completed', 'Completed'
    user = models.ForeignKey(DigiUser, on_delete=models.CASCADE, related_name="task_completions")
    task = models.ForeignKey(Task, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    awarded_points = models.PositiveIntegerField(default=0)  # Snapshot of points at completion

    class Meta:
        unique_together = ('user', 'task')  # Prevent duplicates

    def __str__(self):
        return f"{self.user} completed {self.task}"
    
