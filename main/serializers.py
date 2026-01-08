from rest_framework import serializers
from decimal import Decimal
from .models import DigiPass, PassTransaction, Profile, Task, UserTaskCompletion


class UpdateProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model=Profile
        fields = ["id", "names", "email"]
        
class TaskSerializer(serializers.ModelSerializer):
    user_status = serializers.SerializerMethodField()
    class Meta:
        model = Task
        fields = ["id", "title", "description", "points", "icon", "task_type", "external_link" ,"is_active", "user_status"]

    def get_user_status(self, task):
        user = self.context["request"].user
        try:
            user_task = UserTaskCompletion.objects.get(user=user, task=task)
            return user_task.status
        except UserTaskCompletion.DoesNotExist:
            return UserTaskCompletion.Status.PENDING

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
    

# class UserProfileSerializer(serializers.ModelSerializer):
#     rank = serializers.IntegerField(read_only=True)
#     referral_count = serializers.IntegerField(read_only=True)
#     wallet_addr = serializers.CharField(source="user.wallet_address")
#     current_pass_id = serializers.SerializerMethodField()
#     class Meta:
#         model=Profile
#         fields = ["id", "names", "email", "scored_point", "rank", "referral_count", "has_pass", "wallet_addr", "current_pass_id", "referral_code"]

#     def get_current_pass_id(self, obj):
#         user = obj.user
        
#         # Get latest verified or minted pass
#         tx = (
#             user.pass_transactions.filter(is_verified=True, minted=True)
#             .order_by("-created_at").first())

#         if tx and tx.digipass:
#             return tx.digipass.id
#         return None


class UserProfileSerializer(serializers.ModelSerializer):
    wallet_addr = serializers.CharField(source="user.wallet_address")
    current_pass_id = serializers.UUIDField(source="current_pass.id", read_only=True)
    current_pass_power = serializers.IntegerField(source="current_pass.point_power", read_only=True)
    class Meta:
        model = Profile
        fields = ["id", "names", "email", "has_pass", "wallet_addr", "current_pass_id", "current_pass_power", "referral_code"]

class UserProfileStatsSerializer(serializers.Serializer):
    points = serializers.IntegerField()
    rank = serializers.IntegerField()
    referral_count = serializers.IntegerField()

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