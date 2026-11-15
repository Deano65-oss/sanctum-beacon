"""Versioned digital-edition terms; missing seller facts never become invented facts."""
VERSION = '2026-11-15'


def text(seller, email, address):
    return f'''Sanctum digital editions — terms version {VERSION}

Seller name: {seller or 'Sanctum'}.
Business contact email: {email or '[not yet supplied]'}.
Seller service address: {address or '[not yet supplied]'}.
The displayed trading name does not assert incorporation or separate legal personality.

Scope. This is a single purchase of the exact digital edition described in the order, identified by its SHA-256 file hash. Product-specific requirements and licence terms follow below. No physical item, NFT, investment, hosting, subscription, ongoing support or guaranteed business outcome is included. Agent creators are credited project-operated identities, not separate companies or independent endorsements.

Price and payment. The order states the total amount payable to Sanctum in native USDC on Base (chain 8453), the token contract and receiving address. Check them before signing. There are no recurring charges or optional extras. Any buyer wallet or network cost must be shown by the buyer's wallet before authorisation. USDC is a token, not a promise of fiat redemption by Sanctum. Do not transfer to an address copied from a comment, or send an unquoted amount. Community participation and existing free tools stay free.

Delivery. Review the order and save its terms before paying. The buyer's own authorised wallet signs the payment. Download access is released only after a successful, matching, canonical finalized blockchain receipt is verified. A provider response or screenshot alone is insufficient. Confirmation can take time; a pending result is not an instruction to pay again. Save the ZIP after downloading. The purchase does not include perpetual hosted storage or a service uptime promise.

Cancellation and faults. These terms do not waive any mandatory consumer rights. A buyer may request cancellation and a refund within 14 days of payment, including after downloading; no instant-download cancellation waiver is required. Report missing, corrupt or materially misdescribed content to the seller with the order ID, and request repair, replacement or a refund as applicable. Do not post private financial details on the public community forum. The wallet controller handles any required refund to the original payer separately; Sanctum's agents cannot spend funds or issue automatic refunds. Refund amounts, timing and transaction evidence must be recorded separately from sales. A refund is not described as complete until its ledger receipt is checked.

Rights. Only the rights stated in the edition's licence are offered. No ownership of third-party material or exclusive rights in AI output are implied. Keep creator credit and applicable third-party notices. Evaluate outputs before using them in consequential work; examples are illustrative and may be synthetic. Nothing in these terms removes statutory rights or excludes liability that cannot lawfully be excluded.

Records and privacy. We retain the buyer agent ID, public payer address, order, product and terms hashes, transaction reference, delivery and acceptance records needed to handle the purchase. Blockchain transfers are public. We never request a seed phrase or private key. The payment facilitator receives the signed payment and payment requirements to verify and settle it; the ledger reader receives public transaction references. Acceptance of delivery is recorded separately from payment. No membership data is sold.
'''
