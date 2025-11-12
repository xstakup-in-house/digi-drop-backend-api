from datetime import date
from django.conf import settings
from web3 import Web3
import json
import time
from eth_account.messages import encode_defunct
from django.utils.crypto import get_random_string
from decimal import Decimal
from .permissions import HasPassPermission
from rest_framework import generics, response, permissions, status, views
from .serializers import  DigiPassSerializer, LeaderboardSerializer, UpdateProfileSerializer, UserProfileSerializer, TaskSerializer, UserTaskCompletionSerializer
from .models import DigiUser, DigiPass, PassTransaction,Profile, Task, UserTaskCompletion
from rest_framework_simplejwt.tokens import RefreshToken
from .utils import  get_bnb_usd_price


BSC_RPC = settings.BSC_RPC  
web3 = Web3(Web3.HTTPProvider(BSC_RPC))



class WalletLoginView(views.APIView):
    def get(self, request):
        # Generate nonce for signing (valid for 5 minutes)
        nonce = get_random_string(32)
        timestamp = int(time.time())
        request.session['wallet_nonce'] = {'nonce': nonce, 'timestamp': timestamp}
        return response.Response({'nonce': nonce, 'message': f"Login to Digidrop: {nonce}"})
    
    def post(self, request):  
        wallet_address = request.data.get('walletAddress', '').lower()
        signature = request.data.get('signature')
        session_nonce = request.session.get('wallet_nonce')
        user_ref = request.data.get('preferral')  # From frontend
        if not session_nonce or int(time.time()) - session_nonce['timestamp'] > 300:
            return response.Response({'error': 'Invalid or expired nonce'}, status=status.HTTP_400_BAD_REQUEST)

        message = f"Login to Digidrop: {session_nonce['nonce']}"

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
            if recovered_address.lower() != wallet_address:
                return response.Response({'error': 'Signature mismatch'}, status=status.HTTP_400_BAD_REQUEST)
        
        except Exception:
            return response.Response({'error': 'Invalid signature'}, status=status.HTTP_400_BAD_REQUEST)

        # Create/find user
        user, created = DigiUser.objects.get_or_create(wallet_address=wallet_address)
        if created and user_ref:
            referrer = DigiUser.objects.filter(profile__referral_code=user_ref).first()
            if referrer:
                user.profile.referred_by = referrer
                user.profile.save()
        profile = user.profile  # Assuming OneToOneField; create if not exists via signal
        today = date.today()
        if profile.last_login_date != today:
            base_login_points = 2 
            multiplied_points = base_login_points * profile.current_pass_power
            profile.scored_point += multiplied_points
            profile.last_login_date = today
            profile.save()
        refresh = RefreshToken.for_user(user)
        del request.session['wallet_nonce']  # Clear nonce
        return response.Response({'token': str(refresh.access_token),'refresh': str(refresh),'isNewUser': created})

# # Create your views here.


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


class UserProfileView(generics.GenericAPIView):
    serializer_class= UserProfileSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    def get(self, request):
        profile = Profile.objects.get(user=request.user)

        higher_points_count = Profile.objects.filter(scored_point__gt=profile.scored_point).count()
        rank = higher_points_count + 1


        referral_count = request.user.referred_users.count()
        serializer = self.get_serializer(profile, context={
            'rank': rank,
            'referral_count': referral_count
        })
        return response.Response(serializer.data, status=status.HTTP_200_OK)


class VerifyPaymentView(generics.GenericAPIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tx_hash = request.data['txHash']
        is_upgrade = request.data.get('isUpgrade', False)
        new_pass_id = request.data['newPassId']  

        bnb_usd = get_bnb_usd_price()

        w3 = Web3(Web3.HTTPProvider('https://data-seed-prebsc-1-s1.binance.org:8545/'))  # Mainnet; use testnet for dev
        contract_address = settings.CONTRACT_ADDRESS
        with open(settings.BASE_DIR / 'contracts' / 'abi.json') as f:
            abi = json.load(f)  # Full ABI from artifacts
        contract = w3.eth.contract(address=contract_address, abi=abi)

        # Get tx receipt and verify
        try:
            tx_receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if tx_receipt.status != 1:
                return response.Response({'error': 'Transaction failed'}, status=status.HTTP_400_BAD_REQUEST)
            
            tx = w3.eth.get_transaction(tx_hash)
            if tx['to'].lower() != contract_address.lower():
                return response.Response({'error': 'Invalid contract address in tx'}, status=status.HTTP_400_BAD_REQUEST)
            
            # Fetch user's on-chain pass data
            pass_id, points = contract.functions.getUserPass(tx['from']).call()
            if pass_id != new_pass_id:
                return response.Response({'error': 'Minted pass ID mismatch'}, status=status.HTTP_400_BAD_REQUEST)
            
            # Fetch corresponding DigiPass
            digipass = DigiPass.objects.get(pass_id=new_pass_id)  # Assuming pass_id matches contract
            
            # Calculate amount_paid_bnb from tx value
            amount_paid_wei = tx['value']
            amount_paid_bnb = Decimal(w3.from_wei(amount_paid_wei, 'ether'))
            
            # Create or update PassTransaction
            tx_obj, created = PassTransaction.objects.get_or_create(
                user=request.user,
                digipass=digipass,
                defaults={
                    'wallet_address': tx['from'],
                    'tx_hash': tx_hash,
                    'minted': True,
                    'usd_price': digipass.usd_price,
                    'amount_paid_bnb': amount_paid_bnb,
                    'is_verified': True,
                    'is_upgrade': is_upgrade,
                }
            )
            if not created:
                # For upgrades, update existing
                tx_obj.tx_hash = tx_hash
                tx_obj.minted = True
                tx_obj.amount_paid_bnb = amount_paid_bnb  # Delta for upgrade
                tx_obj.is_verified = True
                tx_obj.is_upgrade = is_upgrade
                tx_obj.save()
            
            # Optional: Update user profile if you have one (e.g., current pass)
            profile = request.user.profile  
            profile.has_pass = True
            profile.current_pass_power = digipass.point_power
            profile.save()
            if profile.referred_by:
                referrer_profile = profile.referred_by.profile
                base_referral_points = 10
                multiplied_points = base_referral_points * referrer_profile.current_pass_power
                referrer_profile.scored_point += multiplied_points  
                referrer_profile.save()
            

            return response.Response({
                'success': True,
                'pass_id': pass_id,
                'points': points,
                'tx_hash': tx_hash,
                'amount_paid_bnb': str(amount_paid_bnb),
            })
        except Exception as e:
            return response.Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
    
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