# Contributing to Creative AI Studio

Thank you for your interest in contributing to Creative AI Studio! This document provides guidelines and instructions for contributing to the project.

## Code of Conduct

Please be respectful and constructive in all interactions. We are committed to providing a welcoming environment for all contributors.

## Getting Started

### Prerequisites

- Python 3.9 or higher
- Node.js 18 or higher
- npm or yarn
- Git

### Setting Up Development Environment

1. Fork the repository
2. Clone your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/Creative_AI_Studio.git
   cd Creative_AI_Studio
   ```

3. Create a new branch for your feature:
   ```bash
   git checkout -b feature/your-feature-name
   # or for bug fixes:
   git checkout -b fix/bug-description
   ```

4. Set up the development environment:
   ```bash
   chmod +x setup.sh
   ./setup.sh
   ```

5. Create a `.env` file from the template:
   ```bash
   cp .env.example .env
   ```

## Development Workflow

### Code Style

- Follow PEP 8 for Python code
- Use meaningful variable and function names
- Add docstrings to functions and classes
- Keep functions focused and modular

### Before Submitting a Pull Request

1. **Run tests locally:**
   ```bash
   pytest -q
   ```

2. **Check code style and types:**
   ```bash
   make lint       # eslint (apps/web) + ruff (core/, generators/)
   make typecheck  # mypy (core/, generators/)
   ```

3. **Verify setup:**
   ```bash
   python scripts/check_local_setup.py
   ```

4. **Build Web UI (if changed):**
   ```bash
   cd apps/web
   npm run build
   cd ../..
   ```

### Commit Messages

Write clear and descriptive commit messages:

```
# Good
Add image generation endpoint with quality scoring

# Bad
fixed stuff
```

Format:
- Start with a verb (Add, Fix, Update, Remove, Refactor)
- Be specific about what changed
- Reference issue numbers if applicable: `Closes #123`

### Pull Request Process

1. Update README.md if needed to reflect changes
2. Add tests for new functionality
3. Ensure all tests pass: `pytest`
4. Provide a clear description of your changes
5. Link any related issues

**Pull Request Template:**
```markdown
## Description
Brief description of the changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Performance improvement

## Testing
Describe how you tested your changes

## Checklist
- [ ] Tests pass locally
- [ ] Code follows style guidelines
- [ ] README updated if needed
- [ ] No unnecessary console logs
```

## Project Structure

```
creative-ai-studio/
├── core/              # Core business logic
│   ├── jobs/          # Job queue system
│   ├── models/        # Model management
│   ├── storage/       # Data persistence
│   ├── projects/      # Project management
│   └── events/        # Event bus
├── generators/        # Media generators
│   ├── image/         # Image generation
│   ├── video/         # Video generation
│   └── audio/         # Audio generation
├── apps/
│   ├── api/           # FastAPI application
│   └── web/           # React frontend
├── tests/             # Test suite
├── scripts/           # Utility scripts
└── docs/              # Documentation
```

## Areas for Contribution

### High Priority
- [ ] Improve test coverage
- [ ] Add type hints to Python code
- [ ] Enhance error handling
- [ ] Documentation improvements

### Medium Priority
- [ ] Performance optimizations
- [ ] UI/UX improvements
- [ ] Additional media format support
- [ ] Extended logging

### Low Priority
- [ ] Code refactoring
- [ ] Configuration improvements
- [ ] Demo content

## Testing Guidelines

- Write tests for new features
- Ensure tests are descriptive
- Use pytest fixtures for common setup
- Test both happy path and error cases

Example:
```python
def test_image_generation_success(mock_model):
    """Test successful image generation"""
    result = generate_image("a cat")
    assert result.status == "success"
    assert result.image_path is not None
```

## Documentation

- Update docstrings for code changes
- Add comments for complex logic
- Update README.md for user-facing changes
- Link to relevant architecture docs

## Reporting Issues

When reporting bugs, include:
- Description of the bug
- Steps to reproduce
- Expected behavior
- Actual behavior
- Environment information (Python version, OS, etc.)
- Error messages or logs

## Questions?

- Check the [documentation](docs/)
- Review existing [issues](../../issues)
- Create a new discussion if needed

---

Thank you for contributing! 🙏
