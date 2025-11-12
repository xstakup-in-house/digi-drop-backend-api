from django.contrib.auth.models import BaseUserManager
from django.utils.translation import gettext_lazy as _



class UserManager(BaseUserManager):
    def create_user(self, wallet_address,password=None, **extra_fields):
        if not wallet_address:
            raise ValueError('Wallet address is required')
        user = self.model(wallet_address=wallet_address.lower(), **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, wallet_address, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(wallet_address, **extra_fields)
   