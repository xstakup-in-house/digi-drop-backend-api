# Changelog - Digidrops Backend API

All notable changes to the Digidrops Django Backend API will be documented in this file.

## [Unreleased] - June 2026

### Added
- **Avatar Support**: Added `avatar_url` to the Profile model, admin configuration, serializers, and generated migration `0007_add_avatar_url_to_profile`.
- **Payment & Passport Verification**: 
  - Integrated contract syncing updates and verify payment endpoint (`verify-payment/`).
  - Added blockchain listener models (`BlockchainListenerState`) to track listener state.
  - Implemented retroactive points multiplier logic inside the pass verifier.
- **Dual Referral Rewards**: Updated referral points system inside [utils.py](file:///C:/Users/user/Downloads/digi-drop-backend-api/main/utils.py):
  - Raised base referral score points to `100` points per referral.
  - Added direct reward mechanics giving `80` Stardust points directly to newly recruited users.

### Fixed
- **Query & Rank Corrections**: Fixed the rank computation query to ensure correct ordering, and exposed the `started_at` timestamp inside the `TaskSerializer`.
- **Webhook Resilience**: Strengthened handling and robustness for Moralis webhook requests and database transactions.
- **Frontend Compatibility**:
  - Aligned endpoint responses with the frontend app expectations.
  - Fixed typo in the referral parameter field name during login (`preferral` -> `referral`).
  - Fixed webhooks URL routing by resolving a double slash issue (`//webhooks/moralis` -> `/webhooks/moralis`).
  - Added CORS configuration support for `localhost:3001` (frontend development host).
  - Fixed syntax error in `ALLOWED_HOSTS` configuration due to a missing comma.
  - Corrected references from `current_pass_power` to nested property `current_pass.point_power`.
  - Added missing dependencies, transactions, and price utility imports (`get_bnb_usd_price`, `transaction`, `IntegrityError`).
