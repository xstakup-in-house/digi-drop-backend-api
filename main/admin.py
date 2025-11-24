import requests
from decimal import Decimal, ROUND_HALF_UP
import json
from django.conf import settings
from django.contrib import admin
from web3 import Web3

from .utils import get_bnb_usd_price
from .models import DigiPass, DigiUser, Profile, PassTransaction,Task, UserTaskCompletion

admin.site.register([DigiUser, Profile, PassTransaction])


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('title', 'points', 'task_type', 'is_active')

@admin.register(UserTaskCompletion)
class UserTaskCompletionAdmin(admin.ModelAdmin):
    list_display = ('user', 'task', 'completed_at', 'awarded_points')
    list_filter = ('task',)

@admin.register(DigiPass)
class DigiPassAdmin(admin.ModelAdmin):
    list_display = ('pass_id','name', 'usd_price', 'point_power')
    actions = ['sync_to_contract']

    def sync_to_contract(self, request, queryset):
        w3 = Web3(Web3.HTTPProvider('https://data-seed-prebsc-1-s1.binance.org:8545/'))  # Mainnet; use testnet for dev
        contract_address = settings.CONTRACT_ADDRESS
        with open(settings.BASE_DIR / 'contracts' / 'abi.json') as f:
            abi = json.load(f)
          
        contract = w3.eth.contract(address=contract_address, abi=abi)
        owner_private_key =settings.PRIVATE_KEY  # Use env: os.environ['OWNER_KEY']
        owner_account = w3.eth.account.from_key(owner_private_key)

        
        bnb_usd = get_bnb_usd_price()

        for pass_obj in queryset:
            pass_id = pass_obj.pass_id  # Use new integer field
            price_bnb = (pass_obj.usd_price / bnb_usd)
            price_bnb = price_bnb * Decimal('1.05')
            price_wei = w3.to_wei(float(price_bnb), 'ether')  # 5% buffer for fluctuations
            points = pass_obj.point_power
            pass_type=pass_obj.pass_type
            # NEW: 4 arguments including name
            tx = contract.functions.setPassDetails(pass_id, pass_type, price_wei, points).build_transaction({
                'chainId': 97,
                'nonce': w3.eth.get_transaction_count(owner_account.address),
                'gas': 300000,
                'gasPrice': w3.to_wei('0.73', 'gwei'),
                'from': owner_account.address,
            })

            signed_tx = owner_account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            self.message_user(request, f'Synced {pass_obj.name} (Tx: {tx_hash.hex()})')
            
            

       
    