"""Detect variables in agent history for reuse"""

import re

from browser_use.agent.views import AgentHistoryList, DetectedVariable
from browser_use.dom.views import DOMInteractedElement

# Regex pattern for explicit task variables: {{name:value}} or {{name}}
# Examples: {{product_url:https://example.com}}, {{promo_code:SAVE20}}, {{city}}
TASK_VARIABLE_PATTERN = re.compile(r'\{\{([a-zA-Z_][a-zA-Z0-9_]*?)(?::([^}]+))?\}\}')


def extract_task_variables(task: str) -> tuple[str, dict[str, DetectedVariable]]:
	"""
	Extract explicit variables from task text using {{name:value}} syntax.

	Syntax options:
	- {{name:value}} - Named variable with explicit value
	- {{name}} - Named placeholder (will need value on replay)

	Examples:
		task = "Go to {{product_url:https://example.com}} and apply {{promo_code:SAVE20}}"
		processed_task, variables = extract_task_variables(task)
		# processed_task = "Go to https://example.com and apply SAVE20"
		# variables = {
		#     'product_url': DetectedVariable(name='product_url', original_value='https://example.com', ...),
		#     'promo_code': DetectedVariable(name='promo_code', original_value='SAVE20', ...),
		# }

	Returns:
		Tuple of (processed_task with values substituted, dict of detected variables)
	"""
	detected: dict[str, DetectedVariable] = {}

	def replace_match(match: re.Match) -> str:
		var_name = match.group(1)
		var_value = match.group(2)  # May be None if using {{name}} syntax

		if var_value is None:
			# Placeholder without value - keep the marker for now
			# User must provide value on replay
			return match.group(0)

		# Detect format from value
		var_format = _detect_format_from_value(var_value)

		# Handle duplicate variable names
		final_name = _ensure_unique_name(var_name, detected)

		detected[final_name] = DetectedVariable(
			name=final_name,
			original_value=var_value,
			type='string',
			format=var_format,
			source='task',  # Mark as coming from task text
		)

		return var_value

	processed_task = TASK_VARIABLE_PATTERN.sub(replace_match, task)

	return processed_task, detected


def substitute_task_variables(task: str, variables: dict[str, str]) -> str:
	"""
	Substitute variables in task text.

	Works with both:
	- {{name:value}} syntax (replaces the whole marker including old value)
	- {{name}} syntax (replaces placeholder with provided value)

	Args:
		task: Original task text with variable markers
		variables: Dict mapping variable names to new values

	Returns:
		Task text with variables substituted
	"""
	def replace_match(match: re.Match) -> str:
		var_name = match.group(1)
		original_value = match.group(2)  # May be None

		if var_name in variables:
			return variables[var_name]
		elif original_value is not None:
			return original_value
		else:
			# Placeholder without value and no substitution provided
			return match.group(0)

	return TASK_VARIABLE_PATTERN.sub(replace_match, task)


def _detect_format_from_value(value: str) -> str | None:
	"""Detect the format hint from a variable value."""
	# URL detection
	if value.startswith(('http://', 'https://', 'www.')):
		return 'url'

	# Email detection
	if '@' in value and '.' in value:
		if re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', value):
			return 'email'

	# Phone detection
	if re.match(r'^[\d\s\-\(\)\+]+$', value):
		digits_only = re.sub(r'[\s\-\(\)\+]', '', value)
		if len(digits_only) >= 10:
			return 'phone'

	# Date detection
	if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
		return 'date'

	return None


def detect_variables_in_history(
	history: AgentHistoryList,
	task_variables: dict[str, DetectedVariable] | None = None,
) -> dict[str, DetectedVariable]:
	"""
	Analyze agent history and detect reusable variables.

	Uses three strategies (in priority order):
	1. Explicit task variables ({{name:value}} syntax) - most reliable, user-specified
	2. Element attributes (id, name, type, placeholder, aria-label) - reliable for form inputs
	3. Value pattern matching (email, phone, date formats) - fallback

	Args:
		history: The agent's action history
		task_variables: Optional pre-extracted task variables from {{name:value}} syntax

	Returns:
		Dictionary mapping variable names to DetectedVariable objects
	"""
	detected: dict[str, DetectedVariable] = {}
	detected_values: set[str] = set()  # Track which values we've already detected

	# STRATEGY 0: Include explicit task variables first (highest priority)
	if task_variables:
		for var_name, var_info in task_variables.items():
			detected[var_name] = var_info
			detected_values.add(var_info.original_value)

	for step_idx, history_item in enumerate(history.history):
		if not history_item.model_output:
			continue

		for action_idx, action in enumerate(history_item.model_output.action):
			# Convert action to dict - handle both Pydantic models and dict-like objects
			if hasattr(action, 'model_dump'):
				action_dict = action.model_dump()
			elif isinstance(action, dict):
				action_dict = action
			else:
				# For SimpleNamespace or similar objects
				action_dict = vars(action)

			# Get the interacted element for this action (if available)
			element = None
			if history_item.state and history_item.state.interacted_element:
				if len(history_item.state.interacted_element) > action_idx:
					element = history_item.state.interacted_element[action_idx]

			# Detect variables in this action
			_detect_in_action(action_dict, element, detected, detected_values)

	return detected


def _detect_in_action(
	action_dict: dict,
	element: DOMInteractedElement | None,
	detected: dict[str, DetectedVariable],
	detected_values: set[str],
) -> None:
	"""Detect variables in a single action using element context"""

	# Extract action type and parameters
	for action_type, params in action_dict.items():
		if not isinstance(params, dict):
			continue

		# Check fields that commonly contain variables
		fields_to_check = ['text', 'query']

		for field in fields_to_check:
			if field not in params:
				continue

			value = params[field]
			if not isinstance(value, str) or not value.strip():
				continue

			# Skip if we already detected this exact value
			if value in detected_values:
				continue

			# Try to detect variable type (with element context)
			var_info = _detect_variable_type(value, element)
			if not var_info:
				continue

			var_name, var_format = var_info

			# Ensure unique variable name
			var_name = _ensure_unique_name(var_name, detected)

			# Add detected variable
			detected[var_name] = DetectedVariable(
				name=var_name,
				original_value=value,
				type='string',
				format=var_format,
			)

			detected_values.add(value)


def _detect_variable_type(
	value: str,
	element: DOMInteractedElement | None = None,
) -> tuple[str, str | None] | None:
	"""
	Detect if a value looks like a variable, using element context when available.

	Priority:
	1. Element attributes (id, name, type, placeholder, aria-label) - most reliable
	2. Value pattern matching (email, phone, date formats) - fallback

	Returns:
		(variable_name, format) or None if not detected
	"""

	# STRATEGY 1: Use element attributes (most reliable)
	if element and element.attributes:
		attr_detection = _detect_from_attributes(element.attributes)
		if attr_detection:
			return attr_detection

	# STRATEGY 2: Pattern matching on value (fallback)
	return _detect_from_value_pattern(value)


def _detect_from_attributes(attributes: dict[str, str]) -> tuple[str, str | None] | None:
	"""
	Detect variable from element attributes.

	Check attributes in priority order:
	1. type attribute (HTML5 input types - most specific)
	2. id, name, placeholder, aria-label (semantic hints)
	"""

	# Check 'type' attribute first (HTML5 input types)
	input_type = attributes.get('type', '').lower()
	if input_type == 'email':
		return ('email', 'email')
	elif input_type == 'tel':
		return ('phone', 'phone')
	elif input_type == 'date':
		return ('date', 'date')
	elif input_type == 'number':
		return ('number', 'number')
	elif input_type == 'url':
		return ('url', 'url')

	# Combine semantic attributes for keyword matching
	semantic_attrs = [
		attributes.get('id', ''),
		attributes.get('name', ''),
		attributes.get('placeholder', ''),
		attributes.get('aria-label', ''),
	]

	combined_text = ' '.join(semantic_attrs).lower()

	# Address detection
	if any(keyword in combined_text for keyword in ['address', 'street', 'addr']):
		if 'billing' in combined_text:
			return ('billing_address', None)
		elif 'shipping' in combined_text:
			return ('shipping_address', None)
		else:
			return ('address', None)

	# Comment/Note detection
	if any(keyword in combined_text for keyword in ['comment', 'note', 'message', 'description']):
		return ('comment', None)

	# Email detection
	if 'email' in combined_text or 'e-mail' in combined_text:
		return ('email', 'email')

	# Phone detection
	if any(keyword in combined_text for keyword in ['phone', 'tel', 'mobile', 'cell']):
		return ('phone', 'phone')

	# Name detection (order matters - check specific before general)
	if 'first' in combined_text and 'name' in combined_text:
		return ('first_name', None)
	elif 'last' in combined_text and 'name' in combined_text:
		return ('last_name', None)
	elif 'full' in combined_text and 'name' in combined_text:
		return ('full_name', None)
	elif 'name' in combined_text:
		return ('name', None)

	# Date detection
	if any(keyword in combined_text for keyword in ['date', 'dob', 'birth']):
		return ('date', 'date')

	# City detection
	if 'city' in combined_text:
		return ('city', None)

	# State/Province detection
	if 'state' in combined_text or 'province' in combined_text:
		return ('state', None)

	# Country detection
	if 'country' in combined_text:
		return ('country', None)

	# Zip code detection
	if any(keyword in combined_text for keyword in ['zip', 'postal', 'postcode']):
		return ('zip_code', 'postal_code')

	# Company detection
	if 'company' in combined_text or 'organization' in combined_text:
		return ('company', None)

	return None


def _detect_from_value_pattern(value: str) -> tuple[str, str | None] | None:
	"""
	Detect variable type from value pattern (fallback when no element context).

	Patterns:
	- Email: contains @ and . with valid format
	- Phone: digits with separators, 10+ chars
	- Date: YYYY-MM-DD format
	- Name: Capitalized word(s), 2-30 chars, letters only
	- Number: Pure digits, 1-9 chars
	"""

	# Email detection - most specific first
	if '@' in value and '.' in value:
		# Basic email validation
		if re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', value):
			return ('email', 'email')

	# Phone detection (digits with separators, 10+ chars)
	if re.match(r'^[\d\s\-\(\)\+]+$', value):
		# Remove separators and check length
		digits_only = re.sub(r'[\s\-\(\)\+]', '', value)
		if len(digits_only) >= 10:
			return ('phone', 'phone')

	# Date detection (YYYY-MM-DD or similar)
	if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
		return ('date', 'date')

	# Name detection (capitalized, only letters/spaces, 2-30 chars)
	if value and value[0].isupper() and value.replace(' ', '').replace('-', '').isalpha() and 2 <= len(value) <= 30:
		words = value.split()
		if len(words) == 1:
			return ('first_name', None)
		elif len(words) == 2:
			return ('full_name', None)
		else:
			return ('name', None)

	# Number detection (pure digits, not phone length)
	if value.isdigit() and 1 <= len(value) <= 9:
		return ('number', 'number')

	return None


def _ensure_unique_name(base_name: str, existing: dict[str, DetectedVariable]) -> str:
	"""
	Ensure variable name is unique by adding suffix if needed.

	Examples:
		first_name → first_name
		first_name (exists) → first_name_2
		first_name_2 (exists) → first_name_3
	"""
	if base_name not in existing:
		return base_name

	# Add numeric suffix
	counter = 2
	while f'{base_name}_{counter}' in existing:
		counter += 1

	return f'{base_name}_{counter}'
