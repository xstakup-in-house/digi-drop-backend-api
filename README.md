# Digidrops Backend API (Django)

This repository hosts the backend API for **Digidrops**, a decentralized network of verified humans contributing trusted signals, reputation, and intelligence for the next generation of Web3 AI. 

Built using Python, Django, and Django REST Framework (DRF), this backend handles authentication (via Web3 wallets), metadata configuration, user profile tracking, quest management, referral rewards processing, on-chain transaction verification, and contract synchronization.

---

## 🛠️ Technology Stack & Dependencies

- **Framework**: Django 4.2
- **REST Layer**: Django REST Framework (DRF) + SimpleJWT (JWT wallet auth)
- **Web3 Integration**: Web3.py (connecting to BSC RPC node, interacting with Soulbound Passports contracts)
- **Database**: SQLite (Development) / PostgreSQL (Production)
- **Storage Services**: Cloudinary (handling profile avatar and asset storage)
- **API Documentation**: Swagger / OpenAPI via `drf-yasg`

---

## 🚀 Core Features

### 1. Web3 Wallet Authentication
- Authentic, passwordless login using a user's BNB Chain wallet.
- The server generates a unique time-sensitive cryptographic nonce via `/login` (`GET`).
- The user signs the login message via their Web3 wallet (e.g. MetaMask).
- The server recovers the signature on-chain to verify ownership and issues a JSON Web Token (JWT) access/refresh token pair.

### 2. Dual Referral Rewards System
- **Referrer Reward**: When user B signs up using user A's referral code and mints a Soulbound Passport, user A receives `100 Stardust points` multiplied by user A's own Passport point power (`point_power`).
- **Referee Reward**: Upon minting their first Passport, the newly referred user B receives `80 Stardust points` directly as a welcome reward.
- Implemented atomically in [utils.py](file:///C:/Users/user/Downloads/digi-drop-backend-api/main/utils.py) via `award_referral_points(profile)`.

### 3. Soulbound Passports (SBTs) & Tiered Multipliers
- **Black Passport**: $3 USD equivalent, `point_power` = 1
- **White Passport**: $5 USD equivalent, `point_power` = 2
- **Gold Passport**: $9 USD equivalent, `point_power` = 4
- These point powers serve as multipliers dynamically increasing Stardust points earned from daily logins and quests.

### 4. Admin Smart Contract Synchronization
- Direct blockchain interface via Django Admin. Administrators can select one or more `DigiPass` objects in the admin panel and run the **`sync_to_contract`** action.
- This action fetches the latest BNB/USD price via the Coinlore API, calculates the `price_wei` value for the passes with a 5% buffer, and issues transactions to update the contract state (`setPassDetails`) on-chain.

### 5. Blockchain Event Listeners & Webhooks
- **Self-Healing Profile Logic**: If a user is verified on the smart contract but their local profile database state is out of sync, fetching the profile page triggers a self-healing on-chain query to fetch contract details and update database records.
- **Moralis Webhook Endpoint**: Fully structured webhook views receive and verify batched `PassMinted` and `PassUpgraded` events using cryptographic HMAC checks to update pass ownership states asynchronously.

---

## ⚙️ Environment Configuration

Create a `.env` file in the root directory:

```env
SECRET_KEY=django-insecure-your-production-secret-key-here
DEBUG=False

# Storage configuration
CLOUD_NAME=your_cloudinary_cloud_name
CLOUDINARY_API_KEY=your_cloudinary_api_key
CLOUDIANRY_API_SECRET=your_cloudinary_api_secret

# Web3 Configuration
BSC_RPC_DEV_URL=https://data-seed-prebsc-1-s1.binance.org:8545/
CONTRACT_ADDR=0xYourContractAddressHere
PRIVATE_KEY=0xYourContractOwnerPrivateKeyHere
WEBHOOK_SECRET=your_moralis_webhook_signature_secret
```

---

## 🏗️ Setup & Local Running

1. **Setup Virtual Environment & Dependencies**:
   ```bash
   python -m venv venv
   .\venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Run Migrations & Check Setup**:
   ```bash
   python manage.py migrate
   python manage.py check
   ```

3. **Create Superuser**:
   ```bash
   python manage.py createsuperuser
   ```

4. **Run Development Server**:
   ```bash
   python manage.py runserver
   ```
   Admin panel will be accessible at: `http://127.0.0.1:8000/admin/`

---

## 📡 API Endpoints

### Authentication
* `GET /api/login/` - Request a secure, random cryptographic nonce.
* `POST /api/login/` - Submit wallet address, signature, and nonce to obtain JWT tokens.

### Profiles & Stats
* `GET /api/profile/` - Retrieve details of the authenticated profile (triggers self-healing check).
* `PUT/PATCH /api/profile/` - Update profile details (requires a valid pass).
* `GET /api/profile/stats/` - Retrieve Stardust points, global ranking, highest points, and referral metrics.

### Passes & Payments
* `GET /api/digi-passes/` - Retrieve all available Soulbound Passport tiers.
* `GET /api/passes/{id}/` - Retrieve details of a specific pass tier.
* `POST /api/verify-payment/` - Confirm an on-chain transaction hash for mints or upgrades.

### Tasks & Leaderboard
* `GET /api/tasks/` - List all active incomplete tasks for the user.
* `POST /api/tasks/{id}/start/` - Mark a quest as started.
* `POST /api/tasks/{id}/completed/` - Process quest completion and award multiplied points.
* `GET /api/leaderboard/` - Return the top 100 profiles ranked by Stardust points.

### Webhooks
* `POST /api/webhooks/moralis` - Process Moralis-forwarded blockchain events (`PassMinted`, `PassUpgraded`).
