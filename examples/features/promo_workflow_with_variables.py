"""
Example: Universal Promo Code Testing with Explicit Variables

This script demonstrates how to use the {{name:value}} syntax for
explicit variable detection, making workflows reusable across different
stores, products, and promo codes.

Key Features:
1. {{variable_name:value}} syntax marks values as substitutable
2. Variables are automatically detected and stored with the history
3. On replay, variables can be substituted with new values
4. Works for ANY type of value (URLs, codes, text, etc.)

Syntax:
- {{product_url:https://example.com}} - Named variable with value
- {{promo_code:SAVE20}} - Another variable
- Multiple occurrences with same name auto-increment: promo_code, promo_code_2, etc.
"""

import asyncio
from pathlib import Path
from browser_use import Agent
from browser_use.llm import ChatBrowserUse


async def discover_workflow(
	product_urls: list[str],
	promo_codes: list[str],
	history_file: str | Path = 'promo_workflow.json',
):
	"""
	Discovery phase: LLM figures out how to test promo codes on products.

	Variables are explicitly marked with {{name:value}} syntax for reliable
	detection and substitution on replay.
	"""

	# Build product list with explicit variable markers
	# Each URL is marked as a variable for substitution
	products_list = '\n'.join(
		f'  - {{{{product_url_{i}:{url}}}}}'
		for i, url in enumerate(product_urls, 1)
	)

	# Build promo codes list with explicit variable markers
	codes_list = ', '.join(
		f'{{{{promo_code_{i}:{code}}}}}'
		for i, code in enumerate(promo_codes, 1)
	)

	task = f"""Test these promo codes on the following products and report which ones work:

PRODUCTS:
{products_list}

PROMO CODES TO TEST: {codes_list}

For each product URL above:
1. Navigate to the product page
2. Add to cart if needed to access promo code input

For each promo code:
a. Apply the promo code
b. Check the result - look for success message, error message, or discount amount
c. Record: code name, whether it worked (yes/no), discount amount if any, error message if any

After testing all codes, provide a summary

OUTPUT FORMAT - End with a summary like this:
```
PROMO CODE RESULTS:
- CODE1: [WORKED/FAILED] - [discount amount or error message]
- CODE2: [WORKED/FAILED] - [discount amount or error message]

BEST CODE: [code name] with [discount amount]
```"""

	llm = ChatBrowserUse()
	agent = Agent(task=task, llm=llm)

	print(f"Discovery: Testing {len(promo_codes)} codes on {len(product_urls)} products...")
	print("=" * 50)

	history = await agent.run(max_steps=50)

	# Save workflow
	history_file = Path(history_file)
	agent.save_history(history_file)
	print(f"\nWorkflow saved to: {history_file}")

	# Show detected variables - now includes explicit task variables!
	detected = agent.detect_variables()
	if detected:
		print("\nDetected variables (can substitute on replay):")
		for name, info in detected.items():
			source_tag = f" [{info.source}]" if hasattr(info, 'source') else ""
			format_tag = f" (format: {info.format})" if info.format else ""
			value_preview = info.original_value[:50] + ('...' if len(info.original_value) > 50 else '')
			print(f"  {name}: '{value_preview}'{format_tag}{source_tag}")
	else:
		print("\nNo variables detected for substitution")

	# Show result
	if history.final_result():
		print("\n" + "=" * 50)
		print("RESULT:")
		print("=" * 50)
		print(history.final_result())

	return history_file


async def replay_workflow(
	history_file: str | Path,
	variables: dict[str, str] | None = None,
):
	"""
	Replay phase: rerun saved workflow with optional variable substitution.

	Pass a dict mapping variable names to new values to test different
	products/promo codes without re-running discovery.
	"""

	history_file = Path(history_file)
	if not history_file.exists():
		print(f"History file not found: {history_file}")
		return

	print(f"Loading workflow: {history_file}")

	llm = ChatBrowserUse()
	agent = Agent(task='', llm=llm)

	if variables:
		print(f"Substituting variables: {variables}")

	print("=" * 50)

	results = await agent.load_and_rerun(
		history_file,
		variables=variables,
		max_retries=3,
		skip_failures=False,
	)

	print("\n" + "=" * 50)
	print("Replay complete!")
	if results and results[-1].is_done:
		print(f"Result: {results[-1].extracted_content}")


async def main():
	# Configuration - original discovery
	product_urls = [
		'https://www.notino.ro/tommy-hilfiger/tommy-girl-forever-eau-de-toilette-pentru-femei/',
		'https://www.notino.ro/gillette/satin-care-bikiny-skin-care-gel-de-spalare-si-de-barbierit-pentru-femei/'
	]
	promo_codes = ['HOTDEAL', 'WELCOME10', 'VIP50']
	history_file = Path('notino_promo_workflow.json')

	# Run discovery (or replay if workflow exists)
	if not history_file.exists():
		print("No saved workflow found. Running discovery...")
		await discover_workflow(product_urls, promo_codes, history_file)
	else:
		print(f"Found existing workflow: {history_file}")
		print("\nOptions:")
		print("  1. Replay with same parameters")
		print("  2. Replay with different products/codes (example)")
		print("  3. Delete workflow and re-discover")
		print("\nReplaying with same parameters...")
		await replay_workflow(history_file)

		# Example: replay with different parameters
		# To test on a different store, uncomment and modify:
		#
		# new_variables = {
		#     'product_url_1': 'https://different-store.com/product1',
		#     'product_url_2': 'https://different-store.com/product2',
		#     'promo_code_1': 'NEWCODE1',
		#     'promo_code_2': 'NEWCODE2',
		#     'promo_code_3': 'NEWCODE3',
		# }
		# await replay_workflow(history_file, variables=new_variables)


if __name__ == '__main__':
	asyncio.run(main())
