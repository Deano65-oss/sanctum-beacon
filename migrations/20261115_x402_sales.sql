-- Prepared only. Apply explicitly after backup and receiver authorisation.
BEGIN;

CREATE TABLE sales_budgets (
	month VARCHAR(7) NOT NULL, 
	attempts INTEGER NOT NULL, 
	PRIMARY KEY (month)
)

;

CREATE TABLE sales_orders (
	id VARCHAR(36) NOT NULL, 
	buyer_id VARCHAR(64) NOT NULL, 
	client_id VARCHAR(64) NOT NULL, 
	product_id VARCHAR(64) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	amount BIGINT NOT NULL, 
	terms_hash VARCHAR(64) NOT NULL, 
	payer VARCHAR(42) NOT NULL, 
	recipient VARCHAR(42) NOT NULL, 
	network VARCHAR(32) NOT NULL, 
	token VARCHAR(42) NOT NULL, 
	created_at INTEGER NOT NULL, 
	expires_at INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	nonce VARCHAR(66), 
	authorization_expires INTEGER, 
	payload_hash VARCHAR(64), 
	tx_hash VARCHAR(66), 
	log_index INTEGER, 
	block_hash VARCHAR(66), 
	delivered_at INTEGER, 
	accepted_at INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (buyer_id, client_id), 
	UNIQUE (network, token, payer, nonce), 
	UNIQUE (network, token, tx_hash, log_index)
)

;

CREATE TABLE sales_products (
	id VARCHAR(64) NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	creator_id VARCHAR(64) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	amount BIGINT NOT NULL, 
	terms TEXT NOT NULL, 
	content BYTEA NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	PRIMARY KEY (id)
)

;
COMMIT;
