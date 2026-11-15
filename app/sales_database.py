"""Separate sales ledger, intentionally NOT auto-created at app startup."""
from sqlalchemy import MetaData, Table, Column, String, Integer, BigInteger, Text, LargeBinary, UniqueConstraint
metadata = MetaData()
products = Table('sales_products', metadata,
    Column('id', String(64), primary_key=True), Column('title', String(120), nullable=False),
    Column('creator_id', String(64), nullable=False), Column('sha256', String(64), nullable=False),
    Column('amount', BigInteger, nullable=False), Column('terms', Text, nullable=False),
    Column('content', LargeBinary, nullable=False), Column('status', String(16), nullable=False))
orders = Table('sales_orders', metadata,
    Column('id', String(36), primary_key=True), Column('buyer_id', String(64), nullable=False),
    Column('client_id', String(64), nullable=False), Column('product_id', String(64), nullable=False),
    Column('sha256', String(64), nullable=False), Column('amount', BigInteger, nullable=False),
    Column('terms_hash', String(64), nullable=False), Column('payer', String(42), nullable=False),
    Column('recipient', String(42), nullable=False), Column('network', String(32), nullable=False),
    Column('token', String(42), nullable=False), Column('created_at', Integer, nullable=False),
    Column('expires_at', Integer, nullable=False), Column('status', String(16), nullable=False),
    Column('nonce', String(66)), Column('authorization_expires', Integer), Column('payload_hash', String(64)),
    Column('tx_hash', String(66)), Column('log_index', Integer), Column('block_hash', String(66)),
    Column('delivered_at', Integer), Column('accepted_at', Integer),
    UniqueConstraint('buyer_id', 'client_id'), UniqueConstraint('network', 'token', 'payer', 'nonce'),
    UniqueConstraint('network', 'token', 'tx_hash', 'log_index'))
budgets = Table('sales_budgets', metadata,
    Column('month', String(7), primary_key=True), Column('attempts', Integer, nullable=False))
