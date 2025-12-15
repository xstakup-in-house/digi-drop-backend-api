from datetime import date
from django.conf import settings
from django.db import transaction, IntegrityError
from web3 import Web3
import json
from django.db.models import F, Window
from django.db.models.functions import Rank
from eth_account.messages import encode_defunct
from django.utils.crypto import get_random_string
from decimal import Decimal
from .permissions import HasPassPermission
from rest_framework import generics, response, permissions, status, views
from .serializers import  DigiPassSerializer, LeaderboardSerializer, UpdateProfileSerializer, UserProfileSerializer, TaskSerializer, UserTaskCompletionSerializer
from .models import DigiUser, DigiPass,LoginNonce, PassTransaction,Profile, Task, UserTaskCompletion
from rest_framework_simplejwt.tokens import RefreshToken
from .utils import  get_bnb_usd_price


BSC_RPC = settings.BSC_RPC  
web3 = Web3(Web3.HTTPProvider(BSC_RPC))



class WalletLoginView(views.APIView):
    def get(self, request):
        # Generate nonce for signing (valid for 5 minutes)
        nonce = get_random_string(32)
        LoginNonce.objects.create(nonce=nonce)
        message = f"Login to Digidrop: {nonce}"
        return response.Response({'nonce': nonce, 'message': message}, status=status.HTTP_200_OK)
    
    def post(self, request):  
        wallet_address = request.data.get('walletAddress', '').strip().lower()
        signature = request.data.get('signature')
        nonce_str = request.data.get('nonce')
        
        user_ref = request.data.get('preferral')  # From frontend

        if not all([wallet_address, signature, nonce_str]):
            return response.Response({'error': 'Missing fields'}, status=400)
        
        try:
            nonce_obj = LoginNonce.objects.get(nonce=nonce_str)
            if nonce_obj.used or nonce_obj.is_expired():
                return response.Response({'error': 'Invalid or expired nonce'}, status=400)
        except LoginNonce.DoesNotExist:
            return response.Response({'error': 'Invalid nonce'}, status=400)

        # Verify signature
        message = f"Login to Digidrop: {nonce_str}"

        # Step 1: Validate address format
        w3 = Web3()
        if not w3.is_checksum_address(wallet_address):
            try:
                wallet_address = w3.to_checksum_address(wallet_address)
            except ValueError:
                return response.Response({'error': 'Invalid wallet address'}, status=status.HTTP_400_BAD_REQUEST)

        # Step 2: Verify signature
        try:
            encoded_msg = encode_defunct(text=message)
            recovered_address = w3.eth.account.recover_message(encoded_msg, signature=signature)
            if recovered_address.lower() != wallet_address.lower():
                return response.Response({'error': 'Signature mismatch'}, status=status.HTTP_400_BAD_REQUEST)
        
        except Exception:
            return response.Response({'error': 'Invalid signature'}, status=status.HTTP_400_BAD_REQUEST)

        nonce_obj.used = True
        nonce_obj.save()
        # Create/find user
        user, created = DigiUser.objects.get_or_create(
            wallet_address__iexact=wallet_address,
            defaults={'wallet_address': wallet_address}
            )
        if created and user_ref:
            referrer = DigiUser.objects.filter(profile__referral_code=user_ref).first()
            if referrer:
                user.profile.referred_by = referrer
                user.profile.save()
        profile = user.profile  # Assuming OneToOneField; create if not exists via signal
        today = date.today()
        if profile.last_login_date != today:
            base_login_points = 10 
            multiplied_points = base_login_points * profile.current_pass_power
            profile.scored_point += multiplied_points
            profile.last_login_date = today
            profile.save()
        refresh = RefreshToken.for_user(user)
        return response.Response({'token': str(refresh.access_token),'refresh': str(refresh),'isNewUser': created}, status=status.HTTP_200_OK)



class PassListEndpoint(generics.ListAPIView):
    serializer_class=DigiPassSerializer
    queryset=DigiPass.objects.all()


class PassDetailEndpoint(generics.RetrieveAPIView):
    serializer_class=DigiPassSerializer
    queryset=DigiPass.objects.all()
    lookup_field= "id"

class UpdateProfileEndpoint(generics.UpdateAPIView):
    serializer_class = UpdateProfileSerializer
    permission_classes = [HasPassPermission]
    queryset=Profile.objects.all()  
    lookup_field = "pk"

class UserProfileView(generics.RetrieveAPIView):
    serializer_class = UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return (
            Profile.objects.select_related("user", "current_pass").get(user=self.request.user)
        )
    

class UserProfileStatsView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = (Profile.objects.annotate(rank=Window(expression=Rank(), order_by=F("scored_point").desc())).values("user_id", "scored_point", "rank"))
        profile_data = qs.get(user_id=request.user.id)
        referral_count = request.user.referred_users.count()

        return response.Response({
            "point": profile_data["scored_point"],
            "rank": profile_data["rank"],
            "referral_count": referral_count,
        })


class VerifyPaymentView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tx_hash = request.data['txHash']
        is_upgrade = request.data.get('isUpgrade', False)
        new_pass_id = request.data['newPassId']  

        bnb_usd = get_bnb_usd_price()

        w3 = Web3(Web3.HTTPProvider(settings.BSC_RPC))  # Mainnet; use testnet for dev
        contract_address = settings.CONTRACT_ADDRESS
        with open(settings.BASE_DIR / 'contracts' / 'abi.json') as f:
            abi = json.load(f)  # Full ABI from artifacts
        contract = w3.eth.contract(address=contract_address, abi=abi)

        # Get tx receipt and verify
        
        tx_receipt = w3.eth.get_transaction_receipt(tx_hash)
        if tx_receipt.status != 1:
            return response.Response({'error': 'Mint Transaction failed'}, status=status.HTTP_400_BAD_REQUEST)
        
        tx = w3.eth.get_transaction(tx_hash)
        if tx['to'].lower() != contract_address.lower():
            return response.Response({'error': 'Invalid contract address in tx'}, status=status.HTTP_400_BAD_REQUEST)
        
        # Fetch user's on-chain pass data
        pass_id, points = contract.functions.getUserPass(tx['from']).call()
        if pass_id != new_pass_id:
            return response.Response({'error': 'Minted pass ID mismatch'}, status=status.HTTP_400_BAD_REQUEST)
        
             # 3️⃣ DB writes — atomic & idempotent
        try:
            with transaction.atomic():
                # If tx already verified, exit cleanly
                existing = PassTransaction.objects.select_for_update().filter(
                    tx_hash=tx_hash
                ).first()

                if existing and existing.is_verified:
                    return response.Response({"success": True})

                digipass = DigiPass.objects.get(pass_id=new_pass_id)

                tx_obj = existing or PassTransaction(
                    tx_hash=tx_hash,
                    user=request.user,
                )

                tx_obj.wallet_address = tx["from"]
                tx_obj.digipass = digipass
                tx_obj.minted = True
                tx_obj.is_verified = True
                tx_obj.amount_paid_bnb = Decimal(
                    w3.from_wei(tx["value"], "ether")
                )
                tx_obj.usd_price = digipass.usd_price
                tx_obj.is_upgrade = is_upgrade
                tx_obj.save()

                profile = request.user.profile
                profile.current_pass = digipass
                profile.has_pass = True
                profile.save(update_fields=["current_pass", "has_pass"])
                if profile.referred_by:
                    referrer_profile = profile.referred_by.profile
                    base_referral_points = 10
                    multiplied_points = base_referral_points * referrer_profile.current_pass_power
                    referrer_profile.scored_point += multiplied_points  
                    referrer_profile.save()
        except IntegrityError:
            # tx_hash uniqueness race condition
            return response.Response({"success": True})

        return response.Response({
            "success": True,
            "pass_id": pass_id,
            "points": points,
            "tx_hash": tx_hash,
        })
            

        
    
class TaskListView(generics.ListAPIView):
    permission_classes = [HasPassPermission]
    serializer_class = TaskSerializer

    def get_queryset(self):
        # Only incomplete, active tasks for user
        completed_tasks = UserTaskCompletion.objects.filter(user=self.request.user).values_list('task_id', flat=True)
        return Task.objects.filter(is_active=True).exclude(id__in=completed_tasks)
    

class CompleteTaskView(generics.GenericAPIView):
    permission_classes = [HasPassPermission]

    def post(self, request, task_id):
        try:
            task = Task.objects.get(id=task_id, is_active=True)
            if UserTaskCompletion.objects.filter(user=request.user, task=task).exists():
                return response.Response({'error': 'Task already completed'}, status=status.HTTP_400_BAD_REQUEST)
            
            profile = request.user.profile
            multiplied_points = task.points * profile.current_pass_power
            # Verify if on-site (custom logic per task_type/title)
            if task.task_type == 'on_site':
                # e.g., if title == "Complete Profile", check profile fields
                profile = request.user.profile
                if task.title == "Complete Your Profile" and not (profile.names and profile.email):
                    return response.Response({'error': 'Profile not complete'}, status=status.HTTP_400_BAD_REQUEST)
            # For off-site, trust user click (or integrate API verification if possible, e.g., Instagram API for followers—advanced)

            completion = UserTaskCompletion(user=request.user, task=task, awarded_points=multiplied_points)
            completion.save()

            profile = request.user.profile
            profile.scored_point += multiplied_points
            profile.save()

            return response.Response({'success': True, 'awarded_points': multiplied_points})
        except Task.DoesNotExist:
            return response.Response({'error': 'Task not found'}, status=status.HTTP_404_NOT_FOUND)
        

class LeaderboardView(generics.ListAPIView):
    permission_classes = [HasPassPermission]  # Public leaderboard
    serializer_class = LeaderboardSerializer
    pagination_class = None  # No pagination needed for top 100

    def get_queryset(self):
        return Profile.objects.order_by('-scored_point')[:100]  # Top 100 by points desc

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        # Add rank to each (1-based)
        data = []
        for rank, profile in enumerate(queryset, start=1):
            serializer = self.get_serializer(profile, context={'rank': rank})
            data.append(serializer.data)
        return response.Response(data, status=status.HTTP_200_OK)