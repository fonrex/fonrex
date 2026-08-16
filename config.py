#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Configuration for FonRex API.
"""

import os


class Config:
    """Base configuration."""

    # General Configuration
    SECRET_KEY = os.environ.get("SECRET_KEY") or "fonrex-secret-key-change-in-production"
    JSON_SORT_KEYS = False

    # yfinance Configuration
    YFINANCE_TIMEOUT = 30  # Timeout in seconds for yfinance requests

    # Cache Configuration
    CACHE_TTL = 300  # 5 minutes default cache TTL

    # Valid periods configuration
    VALID_PERIODS = {
        "1d": "1d",
        "5d": "5d",
        "1mo": "1mo",
        "3mo": "3mo",
        "6mo": "6mo",
        "1y": "1y",
        "2y": "2y",
        "5y": "5y",
        "10y": "10y",
        "ytd": "ytd",
        "max": "max",
    }

    # Valid output formats configuration
    VALID_FORMATS = ["json", "csv"]

    # Logging Configuration
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    # Rate limiting configuration (for future implementation)
    RATE_LIMIT_PER_MINUTE = 60

    # CORS headers configuration
    CORS_ORIGINS = ["*"]  # To be restricted in production

    # Database Configuration
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get("DATABASE_URL")
        or "postgresql://fonrex:fonrex_password@localhost:5432/fonrex"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": 10,
        "max_overflow": 20,
    }


class DevelopmentConfig(Config):
    """Development configuration."""

    DEBUG = True
    TESTING = False
    # Development database
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get("DATABASE_URL")
        or "postgresql://fonrex:fonrex_password@localhost:5432/fonrex"
    )


class ProductionConfig(Config):
    """Production configuration."""

    DEBUG = False
    TESTING = False

    # In production, use environment variables
    SECRET_KEY = os.environ.get("SECRET_KEY") or Config.SECRET_KEY
    # Commented to allow development without environment variables
    # if not SECRET_KEY:
    #     raise ValueError("SECRET_KEY environment variable must be set in production")


class TestingConfig(Config):
    """Testing configuration."""

    DEBUG = True
    TESTING = True
    CACHE_TTL = 0  # No cache during tests
    # Test database
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get("TEST_DATABASE_URL")
        or "postgresql://fonrex:fonrex_password@localhost:5432/fonrex_test"
    )


# Default configuration
config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
