from rest_framework import serializers
from decimal import Decimal
from .models import DigiPass, PassTransaction, Profile, Task, UserTaskCompletion


class UpdateProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model=Profile
        fields = ["id", "names", "email"]
        
class TaskSerializer(serializers.ModelSerializer):
    class Meta:
        model = Task
        fields = '__all__'

class UserTaskCompletionSerializer(serializers.ModelSerializer):
    task = TaskSerializer(read_only=True)
    class Meta:
        model = UserTaskCompletion
        fields = '__all__'


class DigiPassSerializer(serializers.ModelSerializer):
    bnb_price = serializers.SerializerMethodField()

    class Meta:
        model = DigiPass
        fields = ["pass_id", "id", "name", "usd_price", "pass_type", "point_power", "card", "bnb_price"]

    def get_bnb_price(self, obj):
        from .utils import get_bnb_usd_price
        bnb_usd = get_bnb_usd_price()
        return (obj.usd_price / bnb_usd).quantize(Decimal("0.00000001"))
    

class UserProfileSerializer(serializers.ModelSerializer):
    rank = serializers.IntegerField(read_only=True)
    referral_count = serializers.IntegerField(read_only=True)
    wallet_addr = serializers.CharField(source="user.wallet_address")
    class Meta:
        model=Profile
        fields = ["id", "names", "email", "scored_point", "rank", "referral_count", "has_pass", "wallet_addr", "referral_code"]


class PaymentVerifySerializer(serializers.Serializer):
    txHash = serializers.CharField()
    passId = serializers.IntegerField()
    is_upgrade = serializers.BooleanField(default=False)

    
        
class PassPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = PassTransaction
        fields = ["id", "digipass", "usd_price", "amount_paid_bnb", "tx_hash", "wallet_address", "is_verified", "created_at"]

class LeaderboardSerializer(serializers.ModelSerializer):
    wallet = serializers.CharField(source='user.wallet_address')  # Use wallet as display name
    rank = serializers.SerializerMethodField()  # Custom rank

    class Meta:
        model = Profile
        fields = ['wallet', 'names', 'scored_point', 'rank']

    def get_rank(self, obj):
        return self.context.get('rank', None)