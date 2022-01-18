from django.test import TestCase

from plantit.task_configuration import validate_task_configuration


class TasksUtilsTests(TestCase):
    def test_validate_config_when_is_not_valid_missing_name(self):
        result = validate_task_configuration({
            'author': 'Computational Plant Science Lab',
            'image': 'docker://alpine',
            'commands': 'echo "Hello, world!"'
        })
        self.assertFalse(result[0])
        self.assertTrue('Missing attribute \'name\'' in result[1])

    def test_validate_config_when_is_not_valid_missing_author(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'image': 'docker://alpine',
            'commands': 'echo "Hello, world!"'
        })
        self.assertFalse(result[0])
        self.assertTrue('Missing attribute \'author\'' in result[1])

    def test_validate_config_when_is_not_valid_missing_image(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'commands': 'echo "Hello, world!"'
        })
        self.assertFalse(result[0])
        self.assertTrue('Missing attribute \'image\'' in result[1])

    def test_validate_config_when_is_not_valid_missing_commands(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': 'docker://alpine',
        })
        self.assertFalse(result[0])
        self.assertTrue('Missing attribute \'commands\'' in result[1])

    def test_validate_config_when_is_not_valid_name_wrong_type(self):
        result = validate_task_configuration({
            'name': True,
            'author': 'Computational Plant Science Lab',
            'image': 'docker://alpine',
            'commands': 'echo "Hello, world!"'
        })
        self.assertFalse(result[0])
        self.assertTrue('Attribute \'name\' must be a str' in result[1])

    # def test_validate_config_when_is_not_valid_author_wrong_type(self):
    #     result = validate_task_configuration({
    #         'name': 'Test Flow',
    #         'author': True,
    #         'image': 'docker://alpine',
    #         'commands': 'echo "Hello, world!"'
    #     }, Token.get())
    #     self.assertFalse(result[0])
    #     self.assertTrue('Attribute \'author\' must be a str' in result[1])

    def test_validate_config_when_is_not_valid_image_wrong_type(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': True,
            'commands': 'echo "Hello, world!"'
        })
        self.assertFalse(result[0])
        self.assertTrue('Attribute \'image\' must be a str' in result[1])

    def test_validate_config_when_is_not_valid_commands_wrong_type(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': 'docker://alpine',
            'commands': True
        })
        self.assertFalse(result[0])
        self.assertTrue('Attribute \'commands\' must be a str' in result[1])

    def test_validate_config_when_is_not_valid_mount_wrong_type(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': True,
            'commands': 'echo "Hello, world!"',
            'mount': True,
        })
        self.assertFalse(result[0])
        self.assertTrue('Attribute \'mount\' must be a list' in result[1])

    def test_validate_config_when_is_not_valid_mount_none(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': True,
            'commands': 'echo "Hello, world!"',
            'mount': None,
        })
        self.assertFalse(result[0])
        self.assertTrue('Attribute \'mount\' must be a list' in result[1])

    def test_validate_config_when_is_not_valid_mount_empty(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': True,
            'commands': 'echo "Hello, world!"',
            'mount': [],
        })
        self.assertFalse(result[0])
        self.assertTrue('Attribute \'mount\' must not be empty' in result[1])

    def test_validate_config_when_is_valid_with_no_input_or_output(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': 'docker://alpine',
            'commands': 'echo "Hello, world!"'
        })
        self.assertTrue(result)

    def test_validate_config_when_is_valid_with_no_input_and_empty_file_output(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': 'docker://alpine',
            'commands': 'echo "Hello, world!"',
            'output': {
                'path': '',
            }
        })
        self.assertTrue(result)

    def test_validate_config_when_is_valid_with_no_input_and_nonempty_output(self):
        result = validate_task_configuration({
            'name': 'Test Flow',
            'author': 'Computational Plant Science Lab',
            'image': 'docker://alpine',
            'commands': 'echo "Hello, world!"',
            'output': {
                'path': 'outputdir',
            }
        })
        self.assertTrue(result)