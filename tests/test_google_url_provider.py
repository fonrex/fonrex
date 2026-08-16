import asyncio
import os
import sys

# Ensure we can import from the project root
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from financials.providers.GoogleFinance_provider import GoogleFinanceProvider


async def main():
    provider = GoogleFinanceProvider()

    print("Verifying Google Finance URL...")

    ticker = "AXP"
    result = await provider.get_financials(ticker)

    if not result:
        print("❌ Failed to get data for AXP")
        sys.exit(1)

    print(f"Provider URL: {result.provider_url}")

    if not result.provider_url:
        print("❌ provider_url is missing!")
        sys.exit(1)

    if "google.com/finance/quote" not in result.provider_url:
        print("❌ provider_url format incorrect!")
        sys.exit(1)

    # Check if ticker part exists in URL (AXP or similar)
    # The provider might resolve AXP to something else like NASDAQ:AXP or NYSE:AXP
    if "AXP" not in result.provider_url:
        print(f"❌ Ticker AXP not found in URL: {result.provider_url}")

    print(f"✅ Success! URL: {result.provider_url}")


if __name__ == "__main__":
    asyncio.run(main())
