"""Tests for PlaywrightAI - selector-based actions with AI fallbacks."""

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser.profile import BrowserProfile
from browser_use.browser.session import BrowserSession
from browser_use.playwright_fallback import (
	PlaywrightAI,
	SelectorError,
	_generate_css_selector,
	generate_playwright_script,
)


@pytest.fixture
def test_page_html() -> str:
	return """
	<!DOCTYPE html>
	<html>
	<head><title>Test Page</title></head>
	<body>
		<h1>PlaywrightAI Test</h1>
		<button id="submit-btn" class="primary">Submit</button>
		<a id="apply-promo" href="#promo">Apply Promo</a>
		<input id="email" type="email" placeholder="Enter email">
		<input id="search" type="text" placeholder="Search...">
		<div id="result" style="display:none">Success!</div>
		<script>
			document.getElementById('submit-btn').onclick = function() {
				document.getElementById('result').style.display = 'block';
			};
			document.getElementById('apply-promo').onclick = function(e) {
				e.preventDefault();
				document.body.innerHTML += '<div id="promo-applied">Promo Applied!</div>';
			};
		</script>
	</body>
	</html>
	"""


@pytest.fixture
def server(httpserver: HTTPServer, test_page_html: str):
	httpserver.expect_request('/').respond_with_data(test_page_html, content_type='text/html')
	return httpserver


@pytest.fixture
async def session():
	"""Create a headless browser session for testing."""
	browser_session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
		)
	)
	await browser_session.start()
	yield browser_session
	await browser_session.kill()
	await browser_session.event_bus.stop(clear=True, timeout=5)


async def test_click_by_selector(server: HTTPServer, session: BrowserSession):
	"""Test clicking an element by CSS selector."""
	br = PlaywrightAI(session, agent=None, auto_fallback=False)

	await br.goto(server.url_for('/'))

	# Click the submit button by selector
	result = await br.click('button#submit-btn')
	assert result is True

	# Verify the click worked by checking the result div is visible
	is_visible = await br.evaluate(
		'document.getElementById("result").style.display !== "none"'
	)
	assert is_visible is True


async def test_click_by_id_selector(server: HTTPServer, session: BrowserSession):
	"""Test clicking a link by ID selector."""
	br = PlaywrightAI(session, agent=None, auto_fallback=False)

	await br.goto(server.url_for('/'))

	# Click the apply promo link
	result = await br.click('a#apply-promo')
	assert result is True

	# Verify promo was applied
	await asyncio.sleep(0.1)  # Small delay for DOM update
	promo_exists = await br.evaluate('document.getElementById("promo-applied") !== null')
	assert promo_exists is True


async def test_click_missing_selector_raises(server: HTTPServer, session: BrowserSession):
	"""Test that missing selector raises SelectorError when no fallback."""
	br = PlaywrightAI(session, agent=None, auto_fallback=False)

	await br.goto(server.url_for('/'))

	# This selector doesn't exist
	with pytest.raises(SelectorError, match='Selector failed'):
		await br.click('button#nonexistent')


async def test_fill_by_selector(server: HTTPServer, session: BrowserSession):
	"""Test filling an input by CSS selector."""
	br = PlaywrightAI(session, agent=None, auto_fallback=False)

	await br.goto(server.url_for('/'))

	# Fill the email input
	result = await br.fill('input#email', 'test@example.com')
	assert result is True

	# Verify the value was set
	value = await br.evaluate('document.getElementById("email").value')
	assert value == 'test@example.com'


async def test_wait_for_selector(server: HTTPServer, session: BrowserSession):
	"""Test waiting for a selector to appear."""
	br = PlaywrightAI(session, agent=None)

	await br.goto(server.url_for('/'))

	# Wait for an existing element
	found = await br.wait_for_selector('button#submit-btn', timeout_ms=1000)
	assert found is True

	# Wait for a non-existent element (should timeout)
	not_found = await br.wait_for_selector('#does-not-exist', timeout_ms=500)
	assert not_found is False


async def test_evaluate_js(server: HTTPServer, session: BrowserSession):
	"""Test evaluating JavaScript."""
	br = PlaywrightAI(session, agent=None)

	await br.goto(server.url_for('/'))

	# Get page title
	title = await br.evaluate('document.title')
	assert title == 'Test Page'

	# Count elements
	count = await br.evaluate('document.querySelectorAll("input").length')
	assert count == 2


async def test_class_selector(server: HTTPServer, session: BrowserSession):
	"""Test clicking by class selector."""
	br = PlaywrightAI(session, agent=None, auto_fallback=False)

	await br.goto(server.url_for('/'))

	# Click by class
	result = await br.click('button.primary')
	assert result is True


async def test_complex_selector(server: HTTPServer, session: BrowserSession):
	"""Test clicking with complex CSS selectors."""
	br = PlaywrightAI(session, agent=None, auto_fallback=False)

	await br.goto(server.url_for('/'))

	# Complex selector with multiple conditions
	result = await br.click('input[type="email"]')
	assert result is True


# =============================================================================
# Tests for CSS Selector Generation
# =============================================================================


@dataclass
class MockDOMElement:
	"""Mock DOMInteractedElement for testing selector generation."""

	node_name: str
	attributes: dict[str, str] | None


def test_selector_generation_with_id():
	"""Test that ID selector is preferred."""
	element = MockDOMElement(node_name='button', attributes={'id': 'submit-btn', 'class': 'primary'})
	selector = _generate_css_selector(element)  # type: ignore
	assert selector == 'button#submit-btn'


def test_selector_generation_skips_dynamic_id():
	"""Test that dynamic IDs (with hashes) are skipped."""
	element = MockDOMElement(node_name='div', attributes={'id': 'component-abc123def456', 'class': 'container'})
	selector = _generate_css_selector(element)  # type: ignore
	# Should fall through to class-based selector since ID looks dynamic
	assert selector is None or 'container' in selector


def test_selector_generation_with_data_testid():
	"""Test data-testid selector generation."""
	element = MockDOMElement(node_name='button', attributes={'data-testid': 'login-button'})
	selector = _generate_css_selector(element)  # type: ignore
	assert selector == 'button[data-testid="login-button"]'


def test_selector_generation_with_name():
	"""Test name attribute selector generation."""
	element = MockDOMElement(node_name='input', attributes={'name': 'email', 'type': 'email'})
	selector = _generate_css_selector(element)  # type: ignore
	assert selector == 'input[name="email"]'


def test_selector_generation_with_input_type_placeholder():
	"""Test input selector with type and placeholder."""
	element = MockDOMElement(
		node_name='input', attributes={'type': 'text', 'placeholder': 'Search...'}
	)
	selector = _generate_css_selector(element)  # type: ignore
	assert selector == 'input[type="text"][placeholder="Search..."]'


def test_selector_generation_with_href():
	"""Test link selector with href."""
	element = MockDOMElement(node_name='a', attributes={'href': '/products/item-123'})
	selector = _generate_css_selector(element)  # type: ignore
	assert selector == 'a[href*="/products/item-123"]'


def test_selector_generation_with_class():
	"""Test class-based selector generation."""
	element = MockDOMElement(node_name='div', attributes={'class': 'card product-card featured'})
	selector = _generate_css_selector(element)  # type: ignore
	assert selector == 'div.card.product-card'


def test_selector_generation_filters_utility_classes():
	"""Test that Tailwind-style utility classes are filtered out."""
	element = MockDOMElement(
		node_name='button', attributes={'class': 'bg-blue-500 hover:bg-blue-700 submit-button p-4'}
	)
	selector = _generate_css_selector(element)  # type: ignore
	# Should use submit-button, not utility classes
	assert selector == 'button.submit-button'


def test_selector_generation_with_no_attributes():
	"""Test handling of element with no usable attributes."""
	element = MockDOMElement(node_name='span', attributes={})
	selector = _generate_css_selector(element)  # type: ignore
	assert selector is None


def test_selector_generation_with_none_element():
	"""Test handling of None element."""
	selector = _generate_css_selector(None)  # type: ignore
	assert selector is None


# =============================================================================
# Tests for Script Generation
# =============================================================================


def test_generate_playwright_script_empty_history():
	"""Test script generation with empty history."""
	# Create a minimal mock history
	mock_history = MagicMock()
	mock_history.history = []

	script = generate_playwright_script(mock_history, task='Test task')

	assert 'Auto-generated PlaywrightAI test script' in script
	assert 'Task: Test task' in script
	assert 'async def main()' in script
	assert 'PlaywrightAI(session, agent)' in script
	assert 'asyncio.run(main())' in script


def test_generate_playwright_script_with_click_action():
	"""Test script generation with a click action (no element lookup)."""
	# Create mock history with a click action
	# Note: We use an empty selector_map to test the AI fallback path
	mock_action = MagicMock()
	mock_action.model_dump.return_value = {'click': {'index': 1}}
	mock_action.get_index.return_value = 1

	mock_model_output = MagicMock()
	mock_model_output.action = [mock_action]
	mock_model_output.next_goal = 'Click the submit button'

	mock_history_item = MagicMock()
	mock_history_item.model_output = mock_model_output
	mock_history_item.state = MagicMock()
	mock_history_item.state.selector_map = {}  # Empty - will use AI fallback

	mock_history = MagicMock()
	mock_history.history = [mock_history_item]

	script = generate_playwright_script(mock_history, task='Submit form')

	assert 'Submit form' in script
	# Should fall back to AI since no element in selector_map
	assert 'Click the submit button' in script


def test_generate_playwright_script_with_navigate_action():
	"""Test script generation with a navigate action."""
	mock_action = MagicMock()
	mock_action.model_dump.return_value = {'navigate': {'url': 'https://example.com'}}
	mock_action.get_index.return_value = None

	mock_model_output = MagicMock()
	mock_model_output.action = [mock_action]
	mock_model_output.next_goal = 'Navigate to example.com'

	mock_history_item = MagicMock()
	mock_history_item.model_output = mock_model_output
	mock_history_item.state = MagicMock()
	mock_history_item.state.selector_map = {}

	mock_history = MagicMock()
	mock_history.history = [mock_history_item]

	script = generate_playwright_script(mock_history)

	assert 'goto' in script
	assert 'https://example.com' in script


def test_generate_playwright_script_with_input_action():
	"""Test script generation with an input action."""
	mock_action = MagicMock()
	mock_action.model_dump.return_value = {'input': {'index': 2, 'text': 'test@example.com'}}
	mock_action.get_index.return_value = 2

	mock_model_output = MagicMock()
	mock_model_output.action = [mock_action]
	mock_model_output.next_goal = 'Enter email address'

	mock_history_item = MagicMock()
	mock_history_item.model_output = mock_model_output
	mock_history_item.state = MagicMock()
	mock_history_item.state.selector_map = {}

	mock_history = MagicMock()
	mock_history.history = [mock_history_item]

	script = generate_playwright_script(mock_history)

	# Should fall back to AI with the goal
	assert 'Enter email address' in script
