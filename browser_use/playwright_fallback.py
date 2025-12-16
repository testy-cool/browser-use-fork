"""Playwright-style API with AI fallbacks.

Provides deterministic selector-based operations that fall back to AI
when selectors fail or aren't provided.

Usage:
    from browser_use import Agent, BrowserSession
    from browser_use.playwright_fallback import PlaywrightAI

    async with BrowserSession() as session:
        agent = Agent(task="", llm=my_llm, browser_session=session)
        br = PlaywrightAI(session, agent)

        # Deterministic selector
        await br.click('a#apply-promo')

        # AI fallback if selector missing/fails
        await br.click('a#apply-promo', fallback='click the apply promo button')

        # Pure AI (no selector)
        await br.ai('click the apply promo button')

Script Generation:
    # Run an agent task, then generate a replayable script
    agent = Agent(task="book a flight", llm=llm, browser_session=session)
    history = await agent.run()

    # Generate PlaywrightAI script from the recorded session
    script = generate_playwright_script(history)
    print(script)
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from browser_use.agent.service import Agent
from browser_use.browser.session import BrowserSession

if TYPE_CHECKING:
	from browser_use.agent.views import AgentHistoryList
	from browser_use.dom.views import DOMInteractedElement

logger = logging.getLogger(__name__)


class SelectorError(Exception):
	"""Raised when a CSS selector fails to find an element."""

	pass


class PlaywrightAI:
	"""Playwright-style browser API with AI fallbacks.

	Primary flow: Try CSS selector via CDP
	Fallback flow: Use AI agent to accomplish the same goal

	This enables writing tests that are:
	- Fast and deterministic when selectors work
	- Self-healing when page structure changes
	"""

	def __init__(
		self,
		browser_session: BrowserSession,
		agent: Agent | None = None,
		timeout_ms: int = 5000,
		auto_fallback: bool = True,
	):
		"""
		Args:
			browser_session: Active browser session
			agent: Agent instance for AI fallbacks (required if using fallbacks)
			timeout_ms: Timeout for selector operations
			auto_fallback: If True, automatically try AI when selector fails
		"""
		self.session = browser_session
		self.agent = agent
		self.timeout_ms = timeout_ms
		self.auto_fallback = auto_fallback

	async def _get_cdp_session(self):
		"""Get active CDP session."""
		return await self.session.get_or_create_cdp_session()

	async def _query_selector(self, selector: str) -> int | None:
		"""Query selector and return nodeId, or None if not found."""
		cdp_session = await self._get_cdp_session()

		doc = await cdp_session.cdp_client.send.DOM.getDocument(
			params={'depth': 1}, session_id=cdp_session.session_id
		)

		result = await cdp_session.cdp_client.send.DOM.querySelector(
			params={'nodeId': doc['root']['nodeId'], 'selector': selector},
			session_id=cdp_session.session_id,
		)

		return result.get('nodeId') if result.get('nodeId') else None

	async def _get_element_center(self, node_id: int) -> tuple[float, float] | None:
		"""Get center coordinates of an element by nodeId."""
		cdp_session = await self._get_cdp_session()

		try:
			box_result = await cdp_session.cdp_client.send.DOM.getBoxModel(
				params={'nodeId': node_id}, session_id=cdp_session.session_id
			)
			box_model = box_result.get('model')
			if not box_model:
				return None

			content = box_model['content']
			x = (content[0] + content[2] + content[4] + content[6]) / 4
			y = (content[1] + content[3] + content[5] + content[7]) / 4
			return (x, y)
		except Exception as e:
			logger.debug(f'Failed to get box model: {e}')
			return None

	async def _click_at_coordinates(self, x: float, y: float) -> None:
		"""Click at specific coordinates using CDP Input."""
		cdp_session = await self._get_cdp_session()

		# Mouse down
		await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
			params={
				'type': 'mousePressed',
				'x': x,
				'y': y,
				'button': 'left',
				'clickCount': 1,
			},
			session_id=cdp_session.session_id,
		)

		# Mouse up
		await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
			params={
				'type': 'mouseReleased',
				'x': x,
				'y': y,
				'button': 'left',
				'clickCount': 1,
			},
			session_id=cdp_session.session_id,
		)

	async def _type_text(self, text: str) -> None:
		"""Type text using CDP Input."""
		cdp_session = await self._get_cdp_session()

		for char in text:
			await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
				params={'type': 'keyDown', 'text': char},
				session_id=cdp_session.session_id,
			)
			await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
				params={'type': 'keyUp', 'text': char},
				session_id=cdp_session.session_id,
			)

	async def _run_ai(self, instruction: str) -> Any:
		"""Run AI agent with a single instruction."""
		if self.agent is None:
			raise ValueError('Agent required for AI fallback. Pass agent= to PlaywrightAI()')

		# Run a single step with the instruction
		# We use a mini-task approach
		original_task = self.agent.task
		self.agent.task = instruction

		try:
			result = await self.agent.step()
			return result
		finally:
			self.agent.task = original_task

	async def click(
		self,
		selector: str | None = None,
		*,
		fallback: str | None = None,
		timeout_ms: int | None = None,
	) -> bool:
		"""Click an element by CSS selector, with optional AI fallback.

		Args:
			selector: CSS selector (e.g., 'a#apply-promo', 'button.submit')
			fallback: Natural language fallback description for AI
			timeout_ms: Override default timeout

		Returns:
			True if click succeeded

		Raises:
			SelectorError: If selector fails and no fallback/auto_fallback

		Examples:
			# Pure selector
			await br.click('button#submit')

			# Selector with AI fallback
			await br.click('button#submit', fallback='click the submit button')

			# Pure AI (no selector)
			await br.click(fallback='click the big green button')
		"""
		timeout = timeout_ms or self.timeout_ms

		# Try selector first
		if selector:
			try:
				node_id = await asyncio.wait_for(
					self._query_selector(selector),
					timeout=timeout / 1000,
				)

				if node_id:
					coords = await self._get_element_center(node_id)
					if coords:
						await self._click_at_coordinates(coords[0], coords[1])
						logger.info(f'✓ Clicked selector: {selector}')
						return True

				logger.warning(f'Selector not found: {selector}')

			except asyncio.TimeoutError:
				logger.warning(f'Selector timeout: {selector}')
			except Exception as e:
				logger.warning(f'Selector error ({selector}): {e}')

		# Fallback to AI
		fallback_instruction = fallback or (f'click the element matching: {selector}' if selector else None)

		if fallback_instruction and (self.auto_fallback or fallback):
			logger.info(f'→ AI fallback: {fallback_instruction}')
			await self._run_ai(fallback_instruction)
			return True

		# No fallback available
		if selector:
			raise SelectorError(f'Selector failed: {selector}')
		raise ValueError('Must provide selector or fallback')

	async def fill(
		self,
		selector: str | None = None,
		text: str = '',
		*,
		fallback: str | None = None,
		clear: bool = True,
	) -> bool:
		"""Fill an input field by CSS selector, with optional AI fallback.

		Args:
			selector: CSS selector for the input element
			text: Text to type
			fallback: Natural language fallback for AI
			clear: Clear existing content first

		Examples:
			await br.fill('input#email', 'user@example.com')
			await br.fill('input#email', 'user@example.com', fallback='type email in the email field')
		"""
		if selector:
			try:
				node_id = await self._query_selector(selector)
				if node_id:
					# Focus the element
					cdp_session = await self._get_cdp_session()
					await cdp_session.cdp_client.send.DOM.focus(
						params={'nodeId': node_id}, session_id=cdp_session.session_id
					)

					# Clear if requested (select all + delete)
					if clear:
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={'type': 'keyDown', 'key': 'a', 'commands': ['selectAll']},
							session_id=cdp_session.session_id,
						)
						await cdp_session.cdp_client.send.Input.dispatchKeyEvent(
							params={'type': 'keyDown', 'key': 'Backspace'},
							session_id=cdp_session.session_id,
						)

					# Type the text
					await self._type_text(text)
					logger.info(f'✓ Filled selector: {selector}')
					return True

				logger.warning(f'Selector not found: {selector}')

			except Exception as e:
				logger.warning(f'Selector error ({selector}): {e}')

		# Fallback to AI
		fallback_instruction = fallback or (f'type "{text}" into the input matching: {selector}' if selector else None)

		if fallback_instruction and (self.auto_fallback or fallback):
			logger.info(f'→ AI fallback: {fallback_instruction}')
			await self._run_ai(fallback_instruction)
			return True

		if selector:
			raise SelectorError(f'Selector failed: {selector}')
		raise ValueError('Must provide selector or fallback')

	async def goto(self, url: str) -> None:
		"""Navigate to a URL."""
		await self.session.navigate_to(url)
		logger.info(f'✓ Navigated to: {url}')

	async def ai(self, instruction: str) -> Any:
		"""Run an AI instruction directly (no selector attempt).

		This is the pure AI path - useful when you don't have a selector
		or want the AI to figure out what to do.

		Examples:
			await br.ai('click the apply promo button')
			await br.ai('scroll down and find the pricing section')
			await br.ai('fill out the contact form with test data')
		"""
		return await self._run_ai(instruction)

	async def wait_for_selector(
		self,
		selector: str,
		*,
		timeout_ms: int | None = None,
		poll_interval_ms: int = 100,
	) -> bool:
		"""Wait for a selector to appear in the DOM.

		Args:
			selector: CSS selector to wait for
			timeout_ms: Max time to wait
			poll_interval_ms: How often to check

		Returns:
			True if found, False if timeout
		"""
		timeout = timeout_ms or self.timeout_ms
		elapsed = 0

		while elapsed < timeout:
			node_id = await self._query_selector(selector)
			if node_id:
				logger.info(f'✓ Selector appeared: {selector}')
				return True

			await asyncio.sleep(poll_interval_ms / 1000)
			elapsed += poll_interval_ms

		logger.warning(f'Timeout waiting for selector: {selector}')
		return False

	async def evaluate(self, js_code: str) -> Any:
		"""Execute JavaScript in the page context.

		Args:
			js_code: JavaScript code to execute

		Returns:
			Result of the JavaScript execution

		Example:
			title = await br.evaluate('document.title')
			count = await br.evaluate('document.querySelectorAll(".item").length')
		"""
		cdp_session = await self._get_cdp_session()

		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': js_code, 'returnByValue': True, 'awaitPromise': True},
			session_id=cdp_session.session_id,
		)

		if result.get('exceptionDetails'):
			raise Exception(f'JS error: {result["exceptionDetails"]}')

		return result.get('result', {}).get('value')


# =============================================================================
# Script Generation - Convert agent history to PlaywrightAI scripts
# =============================================================================


def _generate_css_selector(element: 'DOMInteractedElement') -> str | None:
	"""Generate a CSS selector from a DOM element.

	Prioritizes (in order):
	1. ID selector: #my-id
	2. Unique class + tag: button.submit-btn
	3. Attribute selectors: input[name="email"]
	4. Tag + nth-child fallback
	"""
	if not element or not element.attributes:
		return None

	attrs = element.attributes
	tag = element.node_name.lower()

	# 1. ID is best - unique and stable
	if 'id' in attrs and attrs['id']:
		element_id = attrs['id']
		# Skip dynamic IDs (contain random-looking strings)
		if not re.search(r'[0-9a-f]{8,}|_\d+$|\d{5,}', element_id):
			return f'{tag}#{element_id}'

	# 2. Try data-testid or data-test (test-friendly attributes)
	for test_attr in ['data-testid', 'data-test', 'data-cy']:
		if test_attr in attrs and attrs[test_attr]:
			return f'{tag}[{test_attr}="{attrs[test_attr]}"]'

	# 3. Name attribute (common for forms)
	if 'name' in attrs and attrs['name']:
		return f'{tag}[name="{attrs["name"]}"]'

	# 4. Type + other attribute for inputs
	if tag == 'input' and 'type' in attrs:
		input_type = attrs['type']
		if 'placeholder' in attrs and attrs['placeholder']:
			return f'input[type="{input_type}"][placeholder="{attrs["placeholder"]}"]'
		return f'input[type="{input_type}"]'

	# 5. Href for links
	if tag == 'a' and 'href' in attrs and attrs['href']:
		href = attrs['href']
		# Use partial match for long URLs
		if len(href) > 50:
			# Extract key part of URL
			href = href.split('?')[0].split('#')[0]
		if href and href != '#':
			return f'a[href*="{href[:50]}"]'

	# 6. Class-based selector (less reliable but better than nothing)
	if 'class' in attrs and attrs['class']:
		classes = attrs['class'].split()
		# Filter out utility classes (tailwind, etc.) and dynamic classes
		stable_classes = [
			c for c in classes
			if not re.search(r'^(sm:|md:|lg:|xl:|hover:|focus:|active:|w-|h-|p-|m-|text-|bg-|border-)', c)
			and not re.search(r'[0-9a-f]{6,}|\d{3,}', c)
			and len(c) > 2
		]
		if stable_classes:
			# Use first 2 stable classes max
			class_selector = '.'.join(stable_classes[:2])
			return f'{tag}.{class_selector}'

	# 7. Fall back to XPath-derived selector if available
	# (This is a last resort - prefer other selectors)
	return None


def _escape_string(s: str) -> str:
	"""Escape a string for use in Python code."""
	return s.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')


def _get_action_type(action) -> str:
	"""Extract the action type from an ActionModel."""
	# ActionModel has a method or we can check which field is set
	action_dict = action.model_dump(exclude_none=True)
	# Find the action key (click, input, navigate, etc.)
	for key in action_dict:
		if key not in ('index', 'coordinate_x', 'coordinate_y'):
			return key
	return 'unknown'


def generate_playwright_script(
	history: 'AgentHistoryList',
	task: str | None = None,
	include_comments: bool = True,
	variable_name: str = 'br',
) -> str:
	"""Generate a PlaywrightAI script from agent history.

	Takes the recorded history of an agent run and generates a Python script
	that can replay the same actions using PlaywrightAI, with CSS selectors
	for speed and AI fallbacks for resilience.

	Args:
		history: The AgentHistoryList from agent.run()
		task: Optional task description for the script header
		include_comments: Whether to include goal comments
		variable_name: Name for the PlaywrightAI instance (default: 'br')

	Returns:
		Python script as a string

	Example:
		agent = Agent(task="search for flights", llm=llm, browser_session=session)
		history = await agent.run()
		script = generate_playwright_script(history, task=agent.task)
		print(script)
		# Or save to file:
		Path("test_flight_search.py").write_text(script)
	"""
	lines = [
		'"""Auto-generated PlaywrightAI test script.',
		'',
		f'Task: {task or "Unknown"}',
		'',
		'Generated from browser-use agent recording.',
		'Uses CSS selectors with AI fallbacks for resilience.',
		'"""',
		'',
		'import asyncio',
		'from browser_use import Agent, BrowserSession, PlaywrightAI',
		'from browser_use.llm.openai.chat import ChatOpenAI  # or your preferred LLM',
		'',
		'',
		'async def main():',
		'\t# Initialize browser and AI agent',
		'\tsession = BrowserSession(headless=False)',
		'\tawait session.start()',
		'\t',
		'\ttry:',
		'\t\tllm = ChatOpenAI(model="gpt-4o")',
		'\t\tagent = Agent(task="", llm=llm, browser_session=session)',
		f'\t\t{variable_name} = PlaywrightAI(session, agent)',
		'\t\t',
	]

	step_num = 0
	for history_item in history.history:
		if not history_item.model_output:
			continue

		model_output = history_item.model_output

		# Get interacted elements for this step (already stored in state)
		interacted_elements = history_item.state.interacted_element if history_item.state else []

		for i, action in enumerate(model_output.action):
			step_num += 1
			element = interacted_elements[i] if i < len(interacted_elements) else None

			# Generate selector
			selector = _generate_css_selector(element) if element else None

			# Get the goal/description for AI fallback
			goal = model_output.next_goal or ''

			# Determine action type and generate code
			action_dict = action.model_dump(exclude_none=True)

			# Add comment with goal
			if include_comments and goal:
				lines.append(f'\t\t# Step {step_num}: {goal[:80]}{"..." if len(goal) > 80 else ""}')

			# Handle different action types
			if 'click' in action_dict:
				click_data = action_dict['click']
				if selector:
					lines.append(
						f"\t\tawait {variable_name}.click('{selector}', fallback='{_escape_string(goal)}')"
					)
				else:
					lines.append(f"\t\tawait {variable_name}.ai('{_escape_string(goal)}')")

			elif 'input' in action_dict:
				input_data = action_dict['input']
				text = input_data.get('text', '')
				if selector:
					lines.append(
						f"\t\tawait {variable_name}.fill('{selector}', '{_escape_string(text)}', fallback='{_escape_string(goal)}')"
					)
				else:
					lines.append(f"\t\tawait {variable_name}.ai('{_escape_string(goal)}')")

			elif 'navigate' in action_dict:
				nav_data = action_dict['navigate']
				url = nav_data.get('url', '')
				lines.append(f"\t\tawait {variable_name}.goto('{_escape_string(url)}')")

			elif 'search' in action_dict:
				search_data = action_dict['search']
				query = search_data.get('query', '')
				lines.append(f"\t\tawait {variable_name}.goto('https://www.google.com/search?q={_escape_string(query)}')")

			elif 'scroll' in action_dict:
				scroll_data = action_dict['scroll']
				direction = 'down' if scroll_data.get('down', True) else 'up'
				lines.append(f"\t\tawait {variable_name}.ai('scroll {direction}')")

			elif 'done' in action_dict:
				done_data = action_dict['done']
				done_text = done_data.get('text', 'Task completed')
				lines.append(f"\t\t# Done: {_escape_string(done_text)[:60]}")

			elif 'extract' in action_dict:
				lines.append(f"\t\t# Extract action - handled by AI")
				lines.append(f"\t\tawait {variable_name}.ai('{_escape_string(goal)}')")

			elif 'send_keys' in action_dict:
				keys_data = action_dict['send_keys']
				keys = keys_data.get('keys', '')
				lines.append(f"\t\tawait {variable_name}.ai('press {keys}')")

			else:
				# Generic fallback for unknown actions
				lines.append(f"\t\tawait {variable_name}.ai('{_escape_string(goal)}')")

			lines.append('\t\t')

	# Close the script
	lines.extend([
		'\tfinally:',
		'\t\tawait session.kill()',
		'\t\tawait session.event_bus.stop()',
		'',
		'',
		'if __name__ == "__main__":',
		'\tasyncio.run(main())',
		'',
	])

	return '\n'.join(lines)


def save_playwright_script(
	history: 'AgentHistoryList',
	output_path: str | Path,
	task: str | None = None,
	include_comments: bool = True,
) -> Path:
	"""Generate and save a PlaywrightAI script from agent history.

	Args:
		history: The AgentHistoryList from agent.run()
		output_path: Path to save the script
		task: Optional task description
		include_comments: Whether to include goal comments

	Returns:
		Path to the saved script

	Example:
		history = await agent.run()
		path = save_playwright_script(history, "tests/test_booking.py", task=agent.task)
		print(f"Script saved to: {path}")
	"""
	script = generate_playwright_script(history, task=task, include_comments=include_comments)
	output_path = Path(output_path)
	output_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(script)
	logger.info(f'✓ PlaywrightAI script saved to: {output_path}')
	return output_path
