'use strict';
(() => {
  const root = document.getElementById('receiver-proof');
  if (!root) return;
  const status = document.getElementById('wallet-status');
  const sign = document.getElementById('sign-receiver');
  const output = document.getElementById('wallet-proof');
  let proof;
  document.getElementById('connect-receiver').addEventListener('click', async () => {
    sign.disabled = true; proof = undefined; output.value = '';
    if (!window.ethereum?.request) {
      status.textContent = 'Open this page in a browser with your wallet installed. Never paste a recovery phrase here.';
      return;
    }
    try {
      const accounts = await window.ethereum.request({method: 'eth_requestAccounts'});
      if (!/^0x[0-9a-fA-F]{40}$/.test(accounts?.[0] || '')) throw new Error('Invalid address');
      const nonce = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
      proof = {address: accounts[0], origin: root.dataset.origin, network: 'eip155:8453', issued_at: new Date().toISOString(), nonce};
      proof.message = `Sanctum receiving wallet control proof\nOrigin: ${proof.origin}\nNetwork: ${proof.network}\nAddress: ${proof.address}\nIssued at: ${proof.issued_at}\nNonce: ${proof.nonce}\nThis proves wallet control only. It authorizes no transfer, spending permission, fee or sale.`;
      document.getElementById('wallet-message').textContent = proof.message;
      status.textContent = `Selected receiving address: ${proof.address}. Check it before signing.`;
      sign.disabled = false;
    } catch (_) { status.textContent = 'Wallet connection was declined or unavailable. Nothing has been sent.'; }
  });
  sign.addEventListener('click', async () => {
    if (!proof) return;
    sign.disabled = true;
    try {
      const hex = '0x' + Array.from(new TextEncoder().encode(proof.message), b => b.toString(16).padStart(2, '0')).join('');
      const signature = await window.ethereum.request({method: 'personal_sign', params: [hex, proof.address]});
      output.value = JSON.stringify({...proof, signature}, null, 2);
      status.textContent = 'Control proof prepared locally. Copy the public proof below; it has not been uploaded.';
    } catch (_) { status.textContent = 'Signature declined or unavailable. No payment was requested.'; }
    sign.disabled = false;
  });
})();
