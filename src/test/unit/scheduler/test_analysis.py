# pylint: disable=protected-access,invalid-name,wrong-import-order,use-implicit-booleaness-not-comparison
import gc
import os
from multiprocessing import Queue
from time import sleep
from unittest import TestCase, mock

import pytest

from objects.firmware import Firmware
from scheduler.analysis import MANDATORY_PLUGINS, AnalysisScheduler
from test.common_helper import DatabaseMock, MockFileObject, fake_exit, get_config_for_testing, get_test_data_dir
from test.mock import mock_patch, mock_spy


class AnalysisSchedulerTest(TestCase):

    def setUp(self):
        self.mocked_interface = DatabaseMock()
        self.enter_patch = mock.patch(target='helperFunctions.database.ConnectTo.__enter__', new=lambda _: self.mocked_interface)
        self.enter_patch.start()
        self.exit_patch = mock.patch(target='helperFunctions.database.ConnectTo.__exit__', new=fake_exit)
        self.exit_patch.start()

        config = get_config_for_testing()
        config.add_section('ip_and_uri_finder')
        config.set('ip_and_uri_finder', 'signature_directory', 'analysis/signatures/ip_and_uri_finder/')
        config.set('default_plugins', 'default', 'file_hashes')
        self.tmp_queue = Queue()
        self.sched = AnalysisScheduler(config=config, pre_analysis=lambda *_: None, post_analysis=self.dummy_callback, db_interface=self.mocked_interface)

    def tearDown(self):
        self.sched.shutdown()
        self.tmp_queue.close()

        self.enter_patch.stop()
        self.exit_patch.stop()
        self.mocked_interface.shutdown()
        gc.collect()

    def dummy_callback(self, fw):
        self.tmp_queue.put(fw)


class TestScheduleInitialAnalysis(AnalysisSchedulerTest):

    def test_plugin_registration(self):
        self.assertIn('dummy_plugin_for_testing_only', self.sched.analysis_plugins, 'Dummy plugin not found')

    def test_schedule_firmware_init_no_analysis_selected(self):
        self.sched.shutdown()
        self.sched.process_queue = Queue()
        test_fw = Firmware(binary=b'test')
        self.sched.start_analysis_of_object(test_fw)
        test_fw = self.sched.process_queue.get(timeout=5)
        self.assertEqual(len(test_fw.scheduled_analysis), len(MANDATORY_PLUGINS), 'Mandatory Plugins not selected')
        for item in MANDATORY_PLUGINS:
            self.assertIn(item, test_fw.scheduled_analysis)

    def test_whole_run_analysis_selected(self):
        test_fw = Firmware(file_path=os.path.join(get_test_data_dir(), 'get_files_test/testfile1'))
        test_fw.scheduled_analysis = ['dummy_plugin_for_testing_only']
        self.sched.start_analysis_of_object(test_fw)
        for _ in range(3):  # 3 plugins have to run
            test_fw = self.tmp_queue.get(timeout=10)
        self.assertEqual(len(test_fw.processed_analysis), 3, 'analysis not done')
        self.assertEqual(test_fw.processed_analysis['dummy_plugin_for_testing_only']['1'], 'first result', 'result not correct')
        self.assertEqual(test_fw.processed_analysis['dummy_plugin_for_testing_only']['summary'], ['first result', 'second result'])
        self.assertIn('file_hashes', test_fw.processed_analysis.keys(), 'Mandatory plug-in not executed')
        self.assertIn('file_type', test_fw.processed_analysis.keys(), 'Mandatory plug-in not executed')

    def test_expected_plugins_are_found(self):
        result = self.sched.get_plugin_dict()

        self.assertIn('file_hashes', result.keys(), 'file hashes plugin not found')
        self.assertIn('file_type', result.keys(), 'file type plugin not found')

        self.assertNotIn('dummy_plug_in_for_testing_only', result.keys(), 'dummy plug-in not removed')

    def test_get_plugin_dict_description(self):
        result = self.sched.get_plugin_dict()
        self.assertEqual(result['file_type'][0], self.sched.analysis_plugins['file_type'].DESCRIPTION, 'description not correct')

    def test_get_plugin_dict_flags(self):
        result = self.sched.get_plugin_dict()

        self.assertTrue(result['file_hashes'][1], 'mandatory flag not set')
        self.assertTrue(result['unpacker'][1], 'unpacker plugin not marked as mandatory')

        self.assertTrue(result['file_hashes'][2]['default'], 'default flag not set')
        self.assertFalse(result['file_type'][2]['default'], 'default flag set but should not')

    def test_get_plugin_dict_version(self):
        result = self.sched.get_plugin_dict()
        self.assertEqual(self.sched.analysis_plugins['file_type'].VERSION, result['file_type'][3], 'version not correct')
        self.assertEqual(self.sched.analysis_plugins['file_hashes'].VERSION, result['file_hashes'][3], 'version not correct')

    def test_process_next_analysis_unknown_plugin(self):
        test_fw = Firmware(file_path=os.path.join(get_test_data_dir(), 'get_files_test/testfile1'))
        test_fw.scheduled_analysis = ['unknown_plugin']

        with mock_spy(self.sched, '_start_or_skip_analysis') as spy:
            self.sched._process_next_analysis_task(test_fw)
            assert not spy.was_called(), 'unknown plugin should simply be skipped'

    def test_skip_analysis_because_whitelist(self):
        self.sched.config.set('dummy_plugin_for_testing_only', 'mime_whitelist', 'foo, bar')
        test_fw = Firmware(file_path=os.path.join(get_test_data_dir(), 'get_files_test/testfile1'))
        test_fw.scheduled_analysis = ['file_hashes']
        test_fw.processed_analysis['file_type'] = {'mime': 'text/plain'}
        self.sched._start_or_skip_analysis('dummy_plugin_for_testing_only', test_fw)
        test_fw = self.tmp_queue.get(timeout=10)
        assert 'dummy_plugin_for_testing_only' in test_fw.processed_analysis
        assert 'skipped' in test_fw.processed_analysis['dummy_plugin_for_testing_only']


class TestAnalysisSchedulerBlacklist:

    test_plugin = 'test_plugin'
    file_object = MockFileObject()

    class PluginMock:
        DEPENDENCIES = []

        def __init__(self, blacklist=None, whitelist=None):
            if blacklist:
                self.MIME_BLACKLIST = blacklist
            if whitelist:
                self.MIME_WHITELIST = whitelist

        def shutdown(self):
            pass

    @classmethod
    def setup_class(cls):
        cls.init_patch = mock.patch(target='scheduler.analysis.AnalysisScheduler.__init__', new=lambda *_: None)
        cls.init_patch.start()
        cls.sched = AnalysisScheduler()
        cls.sched.analysis_plugins = {}
        cls.plugin_list = ['no_deps', 'foo', 'bar']
        cls.init_patch.stop()

    def setup(self):
        self.sched.config = get_config_for_testing()

    def test_get_blacklist_and_whitelist_from_plugin(self):
        self.sched.analysis_plugins['test_plugin'] = self.PluginMock(['foo'], ['bar'])
        blacklist, whitelist = self.sched._get_blacklist_and_whitelist_from_plugin('test_plugin')
        assert (blacklist, whitelist) == (['foo'], ['bar'])

    def test_get_blacklist_and_whitelist_from_plugin__missing_in_plugin(self):
        self.sched.analysis_plugins['test_plugin'] = self.PluginMock(['foo'])
        blacklist, whitelist = self.sched._get_blacklist_and_whitelist_from_plugin('test_plugin')
        assert whitelist == []
        assert isinstance(blacklist, list)

    def test_get_blacklist_and_whitelist_from_config(self):
        self._add_test_plugin_to_config()
        blacklist, whitelist = self.sched._get_blacklist_and_whitelist_from_config('test_plugin')
        assert blacklist == ['type1', 'type2']
        assert whitelist == []

    def test_get_blacklist_and_whitelist__in_config_and_plugin(self):
        self._add_test_plugin_to_config()
        self.sched.analysis_plugins['test_plugin'] = self.PluginMock(['foo'], ['bar'])
        blacklist, whitelist = self.sched._get_blacklist_and_whitelist('test_plugin')
        assert blacklist == ['type1', 'type2']
        assert whitelist == []

    def test_get_blacklist_and_whitelist__plugin_only(self):
        self.sched.analysis_plugins['test_plugin'] = self.PluginMock(['foo'], ['bar'])
        blacklist, whitelist = self.sched._get_blacklist_and_whitelist('test_plugin')
        assert (blacklist, whitelist) == (['foo'], ['bar'])

    def test_next_analysis_is_blacklisted__blacklisted(self):
        self.sched.analysis_plugins[self.test_plugin] = self.PluginMock(blacklist=['blacklisted_type'])
        self.file_object.processed_analysis['file_type']['mime'] = 'blacklisted_type'
        blacklisted = self.sched._next_analysis_is_blacklisted(self.test_plugin, self.file_object)
        assert blacklisted is True

    def test_next_analysis_is_blacklisted__not_blacklisted(self):
        self.sched.analysis_plugins[self.test_plugin] = self.PluginMock(blacklist=[])
        self.file_object.processed_analysis['file_type']['mime'] = 'not_blacklisted_type'
        blacklisted = self.sched._next_analysis_is_blacklisted(self.test_plugin, self.file_object)
        assert blacklisted is False

    def test_next_analysis_is_blacklisted__whitelisted(self):
        self.sched.analysis_plugins[self.test_plugin] = self.PluginMock(whitelist=['whitelisted_type'])
        self.file_object.processed_analysis['file_type']['mime'] = 'whitelisted_type'
        blacklisted = self.sched._next_analysis_is_blacklisted(self.test_plugin, self.file_object)
        assert blacklisted is False

    def test_next_analysis_is_blacklisted__not_whitelisted(self):
        self.sched.analysis_plugins[self.test_plugin] = self.PluginMock(whitelist=['some_other_type'])
        self.file_object.processed_analysis['file_type']['mime'] = 'not_whitelisted_type'
        blacklisted = self.sched._next_analysis_is_blacklisted(self.test_plugin, self.file_object)
        assert blacklisted is True

    def test_next_analysis_is_blacklisted__whitelist_precedes_blacklist(self):
        self.sched.analysis_plugins[self.test_plugin] = self.PluginMock(blacklist=['test_type'], whitelist=['test_type'])
        self.file_object.processed_analysis['file_type']['mime'] = 'test_type'
        blacklisted = self.sched._next_analysis_is_blacklisted(self.test_plugin, self.file_object)
        assert blacklisted is False

        self.sched.analysis_plugins[self.test_plugin] = self.PluginMock(blacklist=[], whitelist=['some_other_type'])
        self.file_object.processed_analysis['file_type']['mime'] = 'test_type'
        blacklisted = self.sched._next_analysis_is_blacklisted(self.test_plugin, self.file_object)
        assert blacklisted is True

    def test_get_blacklist_file_type_from_database(self):
        def add_file_type_mock(_, fo):
            fo.processed_analysis['file_type'] = {'mime': 'foo_type'}

        file_object = MockFileObject()
        file_object.processed_analysis.pop('file_type')
        with mock_patch(self.sched, '_add_completed_analysis_results_to_file_object', add_file_type_mock):
            result = self.sched._get_file_type_from_object_or_db(file_object)
            assert result == 'foo_type'

    def _add_test_plugin_to_config(self):
        self.sched.config.add_section('test_plugin')
        self.sched.config.set('test_plugin', 'mime_blacklist', 'type1, type2')


class TestAnalysisSkipping:

    class PluginMock:
        DEPENDENCIES = []

        def __init__(self, version, system_version):
            self.VERSION = version
            self.NAME = 'test plug-in'
            if system_version:
                self.SYSTEM_VERSION = system_version

    class BackendMock:
        def __init__(self, analysis_entry=None):
            self.analysis_entry = analysis_entry if analysis_entry else {}

        def get_specific_fields_of_db_entry(self, *_):
            return self.analysis_entry

        def retrieve_analysis(self, sanitized_dict, **_):  # pylint: disable=no-self-use
            return sanitized_dict

    @classmethod
    def setup_class(cls):
        cls.init_patch = mock.patch(target='scheduler.analysis.AnalysisScheduler.__init__', new=lambda *_: None)
        cls.init_patch.start()

        cls.scheduler = AnalysisScheduler()
        cls.scheduler.analysis_plugins = {}

        cls.init_patch.stop()

    @pytest.mark.parametrize(
        'plugin_version, plugin_system_version, analysis_plugin_version, '
        'analysis_system_version, expected_output', [
            ('1.0', None, '1.0', None, True),
            ('1.1', None, '1.0', None, False),
            ('1.0', None, '1.1', None, True),
            ('1.0', '2.0', '1.0', '2.0', True),
            ('1.0', '2.0', '1.0', '2.1', True),
            ('1.0', '2.1', '1.0', '2.0', False),
            ('1.0', '2.0', '1.0', None, False),
            (' 1.0', '1.1', '1.1', '1.0', False)  # invalid version string
        ]
    )
    def test_analysis_is_already_in_db_and_up_to_date(
            self, plugin_version, plugin_system_version, analysis_plugin_version, analysis_system_version, expected_output):
        plugin = 'foo'
        analysis_entry = {'processed_analysis': {plugin: {
            'plugin_version': analysis_plugin_version, 'system_version': analysis_system_version, 'file_system_flag': False
        }}}
        self.scheduler.db_backend_service = self.BackendMock(analysis_entry)
        self.scheduler.analysis_plugins[plugin] = self.PluginMock(
            version=plugin_version, system_version=plugin_system_version)
        assert self.scheduler._analysis_is_already_in_db_and_up_to_date(plugin, '') == expected_output

    @pytest.mark.parametrize('db_entry', [
        {}, {'plugin': {}}, {'plugin': {'no': 'version'}},
        {'plugin': {'plugin_version': '0', 'system_version': '0', 'failed': 'reason'}}
    ])
    def test_analysis_is_already_in_db_and_up_to_date__incomplete(self, db_entry):
        analysis_entry = {'processed_analysis': db_entry}
        self.scheduler.db_backend_service = self.BackendMock(analysis_entry)
        self.scheduler.analysis_plugins['plugin'] = self.PluginMock(version='1.0', system_version='1.0')
        assert self.scheduler._analysis_is_already_in_db_and_up_to_date('plugin', '') is False

    def test_is_forced_update(self):
        fo = MockFileObject()
        assert self.scheduler._is_forced_update(fo) is False
        fo.force_update = False
        assert self.scheduler._is_forced_update(fo) is False
        fo.force_update = True
        assert self.scheduler._is_forced_update(fo) is True


class TestAnalysisShouldReanalyse:
    class PluginMock:
        DEPENDENCIES = ['plugin_dep']
        VERSION = '1.0'
        NAME = 'plugin_root'

    class BackendMock:
        pass

    @classmethod
    def setup_class(cls):
        cls.init_patch = mock.patch(target='scheduler.analysis.AnalysisScheduler.__init__', new=lambda *_: None)
        cls.init_patch.start()
        cls.scheduler = AnalysisScheduler()
        cls.init_patch.stop()

    @pytest.mark.parametrize('plugin_root_date, plugin_dep_date, is_up_to_date', [
        (10, 20, False),
        (20, 10, True)
    ])
    def test_analysis_is_up_to_date(self, plugin_root_date, plugin_dep_date, is_up_to_date, monkeypatch):
        def _get_analysis_date_mock(plugin_name, uid, backend_db_interface):
            # pylint: disable=unused-argument
            if plugin_name == 'plugin_root':
                return plugin_root_date
            if plugin_name == 'plugin_dep':
                return plugin_dep_date

            assert False

        monkeypatch.setattr('scheduler.analysis._get_analysis_date', _get_analysis_date_mock)
        uid = 'DONT_CARE'
        analysis_db_entry = {'plugin_version': '1.0'}
        plugin_mock = self.PluginMock()

        self.scheduler.db_backend_service = self.BackendMock()

        assert self.scheduler._analysis_is_up_to_date(analysis_db_entry, plugin_mock, uid) == is_up_to_date


class PluginMock:
    def __init__(self, dependencies):
        self.DEPENDENCIES = dependencies


def test_combined_analysis_workload(monkeypatch):
    monkeypatch.setattr(AnalysisScheduler, '__init__', lambda *_: None)
    scheduler = AnalysisScheduler()

    scheduler.analysis_plugins = {}
    dummy_plugin = scheduler.analysis_plugins['dummy_plugin'] = PluginMock([])
    dummy_plugin.in_queue = Queue()  # pylint: disable=attribute-defined-outside-init
    scheduler.process_queue = Queue()
    try:
        assert scheduler.get_combined_analysis_workload() == 0
        scheduler.process_queue.put({})
        for _ in range(2):
            dummy_plugin.in_queue.put({})
        assert scheduler.get_combined_analysis_workload() == 3
    finally:
        sleep(0.1)  # let the queue finish internally to not cause "Broken pipe"
        scheduler.process_queue.close()
        dummy_plugin.in_queue.close()
