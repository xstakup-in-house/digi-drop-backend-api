import time
import json
from django.core.management.base import BaseCommand
from django.conf import settings
from web3 import Web3
from main.services.pass_verifier import (
    process_mint_event,
    process_upgrade_event,
)


class Command(BaseCommand):
    help = "Listen to pass mint and upgrade events"

    def handle(self, *args, **options):
        w3 = Web3(Web3.HTTPProvider(settings.BSC_RPC))

        with open(settings.BASE_DIR / "contracts" / "abi.json") as f:
            abi = json.load(f)

        contract = w3.eth.contract(
            address=settings.CONTRACT_ADDRESS,
            abi=abi,
        )

        mint_filter = contract.events.PassMinted.create_filter(
            fromBlock="latest"
        )
        upgrade_filter = contract.events.PassUpgraded.create_filter(
            fromBlock="latest"
        )

        self.stdout.write(self.style.SUCCESS("Listening for pass events..."))

        while True:
            try:
                for event in mint_filter.get_new_entries():
                    process_mint_event(event)

                for event in upgrade_filter.get_new_entries():
                    process_upgrade_event(event)

                time.sleep(5)

            except Exception as e:
                self.stderr.write(str(e))
                time.sleep(10)
