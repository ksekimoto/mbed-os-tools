#!/usr/bin/env python

"""
mbed SDK
Copyright (c) 2011-2016 ARM Limited

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Author: Przemyslaw Wirkus <Przemyslaw.wirkus@arm.com>
"""

import os
import sys
import random
import optparse
from time import time
from Queue import Queue
from threading import Thread


from mbed_greentea.mbed_test_api import get_test_spec
from mbed_greentea.mbed_test_api import run_host_test
from mbed_greentea.mbed_test_api import log_mbed_devices_properties
from mbed_greentea.mbed_test_api import TEST_RESULTS
from mbed_greentea.mbed_test_api import TEST_RESULT_OK, TEST_RESULT_FAIL
from mbed_greentea.mbed_report_api import exporter_text
from mbed_greentea.mbed_report_api import exporter_testcase_text
from mbed_greentea.mbed_report_api import exporter_json
from mbed_greentea.mbed_report_api import exporter_testcase_junit
from mbed_greentea.mbed_greentea_log import gt_logger
from mbed_greentea.mbed_greentea_dlm import GREENTEA_KETTLE_PATH
from mbed_greentea.mbed_greentea_dlm import greentea_get_app_sem
from mbed_greentea.mbed_greentea_dlm import greentea_update_kettle
from mbed_greentea.mbed_greentea_dlm import greentea_clean_kettle
from mbed_greentea.mbed_yotta_api import get_test_suite_properties
from mbed_greentea.mbed_greentea_hooks import GreenteaHooks
from mbed_greentea.tests_spec import TestBinary
from mbed_greentea.mbed_target_info import get_platform_property

from mbed_greentea.cmake_handlers import list_binaries_for_builds
from mbed_greentea.cmake_handlers import list_binaries_for_targets

try:
    import mbed_lstools
    import mbed_host_tests
except ImportError as e:
    gt_logger.gt_log_err("Not all required Python modules were imported!")
    gt_logger.gt_log_err(str(e))
    gt_logger.gt_log("Check if:")
    gt_logger.gt_log_tab("1. You've correctly installed dependency module using setup tools or pip:")
    gt_logger.gt_log_tab("* python setup.py install", tab_count=2)
    gt_logger.gt_log_tab("* pip install <module-name>", tab_count=2)
    gt_logger.gt_log_tab("2. There are no errors preventing import in dependency modules")
    gt_logger.gt_log_tab("See: https://github.com/ARMmbed/greentea#installing-greentea")
    exit(-2342)

MBED_LMTOOLS = 'mbed_lstools' in sys.modules
MBED_HOST_TESTS = 'mbed_host_tests' in sys.modules

RET_NO_DEVICES = 1001
RET_YOTTA_BUILD_FAIL = -1
LOCAL_HOST_TESTS_DIR = './test/host_tests'  # Used by mbedhtrun -e <dir>


def get_local_host_tests_dir(path):
    """! Forms path to local host tests. Performs additional basic checks if directory exists etc.
    """
    # If specified path exist return path
    if path and os.path.exists(path) and os.path.isdir(path):
        return path
    # If specified path is not set or doesn't exist returns default path
    if not path and os.path.exists(LOCAL_HOST_TESTS_DIR) and os.path.isdir(LOCAL_HOST_TESTS_DIR):
        return LOCAL_HOST_TESTS_DIR
    return None


def print_version(verbose=True):
    """! Print current package version
    """
    import pkg_resources  # part of setuptools
    version = pkg_resources.require("mbed-greentea")[0].version
    if verbose:
        print version
    return version


def create_filtered_test_list(ctest_test_list, test_by_names, skip_test, test_spec=None):
    """! Filters test case list (filtered with switch -n) and return filtered list.
    @ctest_test_list List iof tests, originally from CTestTestFile.cmake in yotta module. Now comes from test specification
    @test_by_names Command line switch -n <test_by_names>
    @skip_test Command line switch -i <skip_test>
    @param test_spec Test specification object loaded with --test-spec switch
    @return
    """

    def filter_names_by_prefix(test_case_name_list, prefix_name):
        """!
        @param test_case_name_list List of all test cases
        @param prefix_name Prefix of test name we are looking for
        @result Set with names of test names starting with 'prefix_name'
        """
        result = list()
        print
        for test_name in test_case_name_list:
            if test_name.startswith(prefix_name):
                result.append(test_name)
        return result

    filtered_ctest_test_list = ctest_test_list
    test_list = None
    invalid_test_names = []
    if filtered_ctest_test_list is None:
        return {}
    elif test_by_names:
        filtered_ctest_test_list = {}   # Subset of 'ctest_test_list'
        test_list = test_by_names.split(',')
        gt_logger.gt_log("test case filter (specified with -n option)")

        for test_name in test_list:
            if test_name.endswith('*'):
                # This 'star-sufix' filter allows users to filter tests with fixed prefixes
                # Example: -n 'TESTS-mbed_drivers* will filter all test cases with name starting with 'TESTS-mbed_drivers'
                for test_name_filtered in filter_names_by_prefix(ctest_test_list.keys(), test_name[:-1]):
                    filtered_ctest_test_list[test_name_filtered] = ctest_test_list[test_name_filtered]
            elif test_name not in ctest_test_list:
                invalid_test_names.append(test_name)
            else:
                gt_logger.gt_log_tab("test filtered in '%s'"% gt_logger.gt_bright(test_name))
                filtered_ctest_test_list[test_name] = ctest_test_list[test_name]
    elif skip_test:
        test_list = skip_test.split(',')
        gt_logger.gt_log("test case filter (specified with -i option)")

        for test_name in test_list:
            if test_name not in ctest_test_list:
                invalid_test_names.append(test_name)
            else:
                gt_logger.gt_log_tab("test '%s' skipped"% gt_logger.gt_bright(test_name))
                del filtered_ctest_test_list[test_name]

    if invalid_test_names:
        opt_to_print = '-n' if test_by_names else 'skip-test'
        gt_logger.gt_log_warn("invalid test case names (specified with '%s' option)"% opt_to_print)
        for test_name in invalid_test_names:
            if test_spec:
                test_spec_name = test_spec.test_spec_filename
                gt_logger.gt_log_warn("test name '%s' not found in '%s' (specified with --test-spec option)"% (gt_logger.gt_bright(test_name),
                    gt_logger.gt_bright(test_spec_name)))
            else:
                gt_logger.gt_log_warn("test name '%s' not found in CTestTestFile.cmake (specified with '%s' option)"% (gt_logger.gt_bright(test_name),
                    opt_to_print))
        gt_logger.gt_log_tab("note: test case names are case sensitive")
        gt_logger.gt_log_tab("note: see list of available test cases below")
        # Print available test suite names (binary names user can use with -n
        if test_spec:
            list_binaries_for_builds(test_spec)
        else:
            list_binaries_for_targets()
    return filtered_ctest_test_list


def main():
    """ Closure for main_cli() function """
    parser = optparse.OptionParser()

    parser.add_option('-t', '--target',
                    dest='list_of_targets',
                    help='You can specify list of yotta targets you want to build. Use comma to separate them.' +
                         'Note: If --test-spec switch is defined this list becomes optional list of builds you want to filter in your test:' +
                         'Comma separated list of builds from test specification. Applicable if --test-spec switch is specified')

    parser.add_option('-n', '--test-by-names',
                    dest='test_by_names',
                    help='Runs only test enumerated it this switch. Use comma to separate test case names.')

    parser.add_option('-i', '--skip-test',
                    dest='skip_test',
                    help='Skip tests enumerated it this switch. Use comma to separate test case names.')

    parser.add_option("-O", "--only-build",
                    action="store_true",
                    dest="only_build_tests",
                    default=False,
                    help="Only build repository and tests, skips actual test procedures (flashing etc.)")

    parser.add_option("-S", "--skip-build",
                    action="store_true",
                    dest="skip_yotta_build",
                    default=False,
                    help="Skip calling 'yotta build' on this module")

    copy_methods_str = "Plugin support: " + ', '.join(mbed_host_tests.host_tests_plugins.get_plugin_caps('CopyMethod'))
    parser.add_option("-c", "--copy",
                    dest="copy_method",
                    help="Copy (flash the target) method selector. " + copy_methods_str,
                    metavar="COPY_METHOD")

    parser.add_option('', '--parallel',
                    dest='parallel_test_exec',
                    default=1,
                    help='Experimental, you execute test runners for connected to your host MUTs in parallel (speeds up test result collection)')

    parser.add_option("-e", "--enum-host-tests",
                    dest="enum_host_tests",
                    help="Define directory with yotta module local host tests. Default: ./test/host_tests")

    parser.add_option('', '--config',
                    dest='verbose_test_configuration_only',
                    default=False,
                    action="store_true",
                    help='Displays connected boards and detected targets and exits.')

    parser.add_option('', '--release',
                    dest='build_to_release',
                    default=False,
                    action="store_true",
                    help='If possible force build in release mode (yotta -r).')

    parser.add_option('', '--debug',
                    dest='build_to_debug',
                    default=False,
                    action="store_true",
                    help='If possible force build in debug mode (yotta -d).')

    parser.add_option('-l', '--list',
                    dest='list_binaries',
                    default=False,
                    action="store_true",
                    help='List available binaries')

    parser.add_option('-m', '--map-target',
                    dest='map_platform_to_yt_target',
                    help='List of custom mapping between platform name and yotta target. Comma separated list of PLATFORM:TARGET tuples')

    parser.add_option('', '--use-tids',
                    dest='use_target_ids',
                    help='Specify explicitly which devices can be used by Greentea for testing by creating list of allowed Target IDs (use comma separated list)')

    parser.add_option('-u', '--shuffle',
                    dest='shuffle_test_order',
                    default=False,
                    action="store_true",
                    help='Shuffles test execution order')

    parser.add_option('', '--shuffle-seed',
                    dest='shuffle_test_seed',
                    default=None,
                    help='Shuffle seed (If you want to reproduce your shuffle order please use seed provided in test summary)')

    parser.add_option('', '--lock',
                    dest='lock_by_target',
                    default=False,
                    action="store_true",
                    help='Use simple resource locking mechanism to run multiple application instances')

    parser.add_option('', '--digest',
                    dest='digest_source',
                    help='Redirect input from where test suite should take console input. You can use stdin or file name to get test case console output')

    parser.add_option('-H', '--hooks',
                    dest='hooks_json',
                    help='Load hooks used drive extra functionality')

    parser.add_option('', '--test-spec',
                    dest='test_spec',
                    help='Test specification generated by build system.')

    parser.add_option('', '--test-cfg',
                    dest='json_test_configuration',
                    help='Pass to host test data with host test configuration')

    parser.add_option('', '--run',
                    dest='run_app',
                    help='Flash, reset and dump serial from selected binary application')

    parser.add_option('', '--report-junit',
                    dest='report_junit_file_name',
                    help='You can log test suite results in form of JUnit compliant XML report')

    parser.add_option('', '--report-text',
                    dest='report_text_file_name',
                    help='You can log test suite results to text file')

    parser.add_option('', '--report-json',
                    dest='report_json',
                    default=False,
                    action="store_true",
                    help='Outputs test results in JSON')

    parser.add_option('', '--report-fails',
                    dest='report_fails',
                    default=False,
                    action="store_true",
                    help='Prints console outputs for failed tests')

    parser.add_option('', '--yotta-registry',
                    dest='yotta_search_for_mbed_target',
                    default=False,
                    action="store_true",
                    help='Use on-line yotta registry to search for compatible with connected mbed devices yotta targets. Default: search is done in yotta_targets directory')

    parser.add_option('-V', '--verbose-test-result',
                    dest='verbose_test_result_only',
                    default=False,
                    action="store_true",
                    help='Prints test serial output')

    parser.add_option('-v', '--verbose',
                    dest='verbose',
                    default=False,
                    action="store_true",
                    help='Verbose mode (prints some extra information)')

    parser.add_option('', '--plain',
                    dest='plain',
                    default=False,
                    action="store_true",
                    help='Do not use colours while logging')

    parser.add_option('', '--version',
                    dest='version',
                    default=False,
                    action="store_true",
                    help='Prints package version and exits')

    parser.description = """This automated test script is used to test mbed SDK 3.0 on mbed-enabled devices with support from yotta build tool"""
    parser.epilog = """Example: mbedgt --target frdm-k64f-gcc"""

    (opts, args) = parser.parse_args()

    cli_ret = 0

    start = time()
    if opts.lock_by_target:
        # We are using Greentea proprietary locking mechanism to lock between platforms and targets
        gt_logger.gt_log("using (experimental) simple locking mechanism")
        gt_logger.gt_log_tab("kettle: %s"% GREENTEA_KETTLE_PATH)
        gt_file_sem, gt_file_sem_name, gt_instance_uuid = greentea_get_app_sem()
        with gt_file_sem:
            greentea_update_kettle(gt_instance_uuid)
            try:
                cli_ret = main_cli(opts, args, gt_instance_uuid)
            except KeyboardInterrupt:
                greentea_clean_kettle(gt_instance_uuid)
                gt_logger.gt_log_err("ctrl+c keyboard interrupt!")
                return(-2)    # Keyboard interrupt
            except:
                greentea_clean_kettle(gt_instance_uuid)
                gt_logger.gt_log_err("unexpected error:")
                gt_logger.gt_log_tab(sys.exc_info()[0])
                raise
            greentea_clean_kettle(gt_instance_uuid)
    else:
        # Standard mode of operation
        # Other instance must provide mutually exclusive access control to platforms and targets
        try:
            cli_ret = main_cli(opts, args)
        except KeyboardInterrupt:
            gt_logger.gt_log_err("ctrl+c keyboard interrupt!")
            return(-2)    # Keyboard interrupt
        except Exception as e:
            gt_logger.gt_log_err("unexpected error:")
            gt_logger.gt_log_tab(str(e))
            raise

    if not any([opts.list_binaries, opts.version]):
        delta = time() - start  # Test execution time delta
        gt_logger.gt_log("completed in %.2f sec"% delta)

    if cli_ret:
        gt_logger.gt_log_err("exited with code %d"% cli_ret)

    return(cli_ret)


def run_test_thread(test_result_queue, test_queue, opts, mut, build, build_path, greentea_hooks):
    test_exec_retcode = 0
    test_platforms_match = 0
    test_report = {}

    while not test_queue.empty():
        try:
            test = test_queue.get(False)
        except Exception as e:
            gt_logger.gt_log_err(str(e))
            break

        test_result = 'SKIPPED'

        disk = mut['mount_point']
        port = mut['serial_port']
        micro = mut['platform_name']
        program_cycle_s = get_platform_property(micro, "program_cycle_s")
        copy_method = opts.copy_method if opts.copy_method else 'shell'
        verbose = opts.verbose_test_result_only
        enum_host_tests_path = get_local_host_tests_dir(opts.enum_host_tests)

        test_platforms_match += 1
        host_test_result = run_host_test(test['image_path'],
                                         disk,
                                         port,
                                         build_path,
                                         mut['target_id'],
                                         micro=micro,
                                         copy_method=copy_method,
                                         program_cycle_s=program_cycle_s,
                                         digest_source=opts.digest_source,
                                         json_test_cfg=opts.json_test_configuration,
                                         enum_host_tests_path=enum_host_tests_path,
                                         verbose=verbose)

        # Some error in htrun, abort test execution
        if isinstance(host_test_result, int):
            # int(host_test_result) > 0 - Call to mbedhtrun failed
            # int(host_test_result) < 0 - Something went wrong while executing mbedhtrun
            gt_logger.gt_log_err("run_test_thread.run_host_test() failed, aborting...")
            break

        # If execution was successful 'run_host_test' return tuple with results
        single_test_result, single_test_output, single_testduration, single_timeout, result_test_cases, test_cases_summary = host_test_result
        test_result = single_test_result

        build_path_abs = os.path.abspath(build_path)

        if single_test_result != TEST_RESULT_OK:
            test_exec_retcode += 1

        if single_test_result in [TEST_RESULT_OK, TEST_RESULT_FAIL]:
            if greentea_hooks:
                # Test was successful
                # We can execute test hook just after test is finished ('hook_test_end')
                format = {
                    "test_name": test['test_bin'],
                    "test_bin_name": os.path.basename(test['image_path']),
                    "image_path": test['image_path'],
                    "build_path": build_path,
                    "build_path_abs": build_path_abs,
                    "build_name": build,
                }
                greentea_hooks.run_hook_ext('hook_test_end', format)

        # Update report for optional reporting feature
        test_suite_name = test['test_bin'].lower()
        if build not in test_report:
            test_report[build] = {}

        if test_suite_name not in test_report[build]:
            test_report[build][test_suite_name] = {}

        if not test_cases_summary and not result_test_cases:
            gt_logger.gt_log_warn("test case summary event not found")
            gt_logger.gt_log_tab("no test case report present, assuming test suite to be a single test case!")

            # We will map test suite result to test case to
            # output valid test case in report

            # Generate "artificial" test case name from test suite name#
            # E.g:
            #   mbed-drivers-test-dev_null -> dev_null
            test_case_name = test_suite_name
            test_str_idx = test_suite_name.find("-test-")
            if test_str_idx != -1:
                test_case_name = test_case_name[test_str_idx + 6:]

            gt_logger.gt_log_tab("test suite: %s"% test_suite_name)
            gt_logger.gt_log_tab("test case: %s"% test_case_name)

            # Test case result: OK, FAIL or ERROR
            tc_result_text = {
                "OK": "OK",
                "FAIL": "FAIL",
            }.get(single_test_result, 'ERROR')

            # Test case integer success code OK, FAIL and ERROR: (0, >0, <0)
            tc_result = {
                "OK": 0,
                "FAIL": 1024,
                "ERROR": -1024,
            }.get(tc_result_text, '-2048')

            # Test case passes and failures: (1 pass, 0 failures) or (0 passes, 1 failure)
            tc_passed, tc_failed = {
                0: (1, 0),
            }.get(tc_result, (0, 1))

            # Test case report build for whole binary
            # Add test case made from test suite result to test case report
            result_test_cases = {
                test_case_name: {
                        'duration': single_testduration,
                        'time_start': 0.0,
                        'time_end': 0.0,
                        'utest_log': single_test_output.splitlines(),
                        'result_text': tc_result_text,
                        'passed': tc_passed,
                        'failed': tc_failed,
                        'result': tc_result,
                    }
            }

            # Test summary build for whole binary (as a test case)
            test_cases_summary = (tc_passed, tc_failed, )

        gt_logger.gt_log("test on hardware with target id: %s"% (mut['target_id']))
        gt_logger.gt_log("test suite '%s' %s %s in %.2f sec"% (test['test_bin'],
            '.' * (80 - len(test['test_bin'])),
            test_result,
            single_testduration))

        # Test report build for whole binary
        test_report[build][test_suite_name]['single_test_result'] = single_test_result
        test_report[build][test_suite_name]['single_test_output'] = single_test_output
        test_report[build][test_suite_name]['elapsed_time'] = single_testduration
        test_report[build][test_suite_name]['platform_name'] = micro
        test_report[build][test_suite_name]['copy_method'] = copy_method
        test_report[build][test_suite_name]['testcase_result'] = result_test_cases

        test_report[build][test_suite_name]['build_path'] = build_path
        test_report[build][test_suite_name]['build_path_abs'] = build_path_abs
        test_report[build][test_suite_name]['image_path'] = test['image_path']
        test_report[build][test_suite_name]['test_bin_name'] = os.path.basename(test['image_path'])

        passes_cnt, failures_cnt = 0, 0
        for tc_name in sorted(result_test_cases.keys()):
            gt_logger.gt_log_tab("test case: '%s' %s %s in %.2f sec"% (tc_name,
                '.' * (80 - len(tc_name)),
                result_test_cases[tc_name].get('result_text', '_'),
                result_test_cases[tc_name].get('duration', 0.0)))
            if result_test_cases[tc_name].get('result_text', '_') == 'OK':
                passes_cnt += 1
            else:
                failures_cnt += 1

        if test_cases_summary:
            passes, failures = test_cases_summary
            gt_logger.gt_log("test case summary: %d pass%s, %d failur%s"% (passes,
                '' if passes == 1 else 'es',
                failures,
                'e' if failures == 1 else 'es'))
            if passes != passes_cnt or failures != failures_cnt:
                gt_logger.gt_log_err("test case summary mismatch: reported passes vs failures miscount!")
                gt_logger.gt_log_tab("(%d, %d) vs (%d, %d)"% (passes, failures, passes_cnt, failures_cnt))

        if single_test_result != 'OK' and not verbose and opts.report_fails:
            # In some cases we want to print console to see why test failed
            # even if we are not in verbose mode
            gt_logger.gt_log_tab("test failed, reporting console output (specified with --report-fails option)")
            print
            print single_test_output

    #greentea_release_target_id(mut['target_id'], gt_instance_uuid)
    test_result_queue.put({'test_platforms_match': test_platforms_match,
                           'test_exec_retcode': test_exec_retcode,
                           'test_report': test_report})
    return


def main_cli(opts, args, gt_instance_uuid=None):
    """! This is main CLI function with all command line parameters
    @details This function also implements CLI workflow depending on CLI parameters inputed
    @return This function doesn't return, it exits to environment with proper success code
    """

    if not MBED_LMTOOLS:
        gt_logger.gt_log_err("error: mbed-ls proprietary module not installed")
        return (-1)

    if not MBED_HOST_TESTS:
        gt_logger.gt_log_err("error: mbed-host-tests proprietary module not installed")
        return (-1)

    # This is how you magically control colours in this piece of art software
    gt_logger.colorful(not opts.plain)

    # Prints version and exits
    if opts.version:
        print_version()
        return (0)

    # Load test specification or print warnings / info messages and exit CLI mode
    test_spec, ret = get_test_spec(opts)
    if not test_spec:
        return ret

    # We will load hooks from JSON file to support extra behaviour during test execution
    greentea_hooks = GreenteaHooks(opts.hooks_json) if opts.hooks_json else None

    # Capture alternative test console inputs, used e.g. in 'yotta test command'
    if opts.digest_source:
        enum_host_tests_path = get_local_host_tests_dir(opts.enum_host_tests)
        host_test_result = run_host_test(None,
                                         None,
                                         None,
                                         None,
                                         None,
                                         hooks=greentea_hooks,
                                         digest_source=opts.digest_source,
                                         enum_host_tests_path=enum_host_tests_path,
                                         verbose=opts.verbose_test_result_only)

        # Some error in htrun, abort test execution
        if isinstance(host_test_result, int):
            # int(host_test_result) > 0 - Call to mbedhtrun failed
            # int(host_test_result) < 0 - Something went wrong while executing mbedhtrun
            return host_test_result

        # If execution was successful 'run_host_test' return tuple with results
        single_test_result, single_test_output, single_testduration, single_timeout, result_test_cases, test_cases_summary = host_test_result
        status = TEST_RESULTS.index(single_test_result) if single_test_result in TEST_RESULTS else -1
        return (status)

    ### Query with mbedls for available mbed-enabled devices
    gt_logger.gt_log("detecting connected mbed-enabled devices...")

    # Detect devices connected to system
    mbeds = mbed_lstools.create()
    mbeds_list = mbeds.list_mbeds_ext()

    ready_mbed_devices = [] # Devices which can be used (are fully detected)

    if mbeds_list:
        gt_logger.gt_log("detected %d device%s"% (len(mbeds_list), 's' if len(mbeds_list) != 1 else ''))
        for mut in mbeds_list:
            if not all(mut.values()):
                gt_logger.gt_log_err("can't detect all properties of the device!")
                for prop in mut:
                    if not mut[prop]:
                        gt_logger.gt_log_tab("property '%s' is '%s'"% (prop, str(mut[prop])))
            else:
                ready_mbed_devices.append(mut)
                gt_logger.gt_log_tab("detected '%s' -> '%s', console at '%s', mounted at '%s', target id '%s'"% (
                    gt_logger.gt_bright(mut['platform_name']),
                    gt_logger.gt_bright(mut['platform_name_unique']),
                    gt_logger.gt_bright(mut['serial_port']),
                    gt_logger.gt_bright(mut['mount_point']),
                    gt_logger.gt_bright(mut['target_id'])
                ))
    else:
        gt_logger.gt_log_err("no devices detected")
        return (RET_NO_DEVICES)

    ### We can filter in only specific target ids
    accepted_target_ids = None
    if opts.use_target_ids:
        gt_logger.gt_log("filtering out target ids not on below list (specified with --use-tids switch)")
        accepted_target_ids = opts.use_target_ids.split(',')
        for tid in accepted_target_ids:
            gt_logger.gt_log_tab("accepting target id '%s'"% gt_logger.gt_bright(tid))

    test_exec_retcode = 0       # Decrement this value each time test case result is not 'OK'
    test_platforms_match = 0    # Count how many tests were actually ran with current settings
    target_platforms_match = 0  # Count how many platforms were actually tested with current settings

    test_report = {}            # Test report used to export to Junit, HTML etc...
    muts_to_test = []           # MUTs to actually be tested
    test_queue = Queue()        # contains information about test_bin and image_path for each test case
    test_result_queue = Queue() # used to store results of each thread
    execute_threads = []        # list of threads to run test cases

    ### check if argument of --parallel mode is a integer and greater or equal 1
    try:
        parallel_test_exec = int(opts.parallel_test_exec)
        if parallel_test_exec < 1:
            parallel_test_exec = 1
    except ValueError:
        gt_logger.gt_log_err("argument of mode --parallel is not a int, disable parallel mode")
        parallel_test_exec = 1

    # Values used to generate random seed for test execution order shuffle
    SHUFFLE_SEED_ROUND = 10 # Value used to round float random seed
    shuffle_random_seed = round(random.random(), SHUFFLE_SEED_ROUND)

    # Set shuffle seed if it is provided with command line option
    if opts.shuffle_test_seed:
        shuffle_random_seed = round(float(opts.shuffle_test_seed), SHUFFLE_SEED_ROUND)

    ### Testing procedures, for each target, for each target's compatible platform
    # In case we are using test spec (switch --test-spec) command line option -t <list_of_targets>
    # is used to enumerate builds from test spec we are suppling
    filter_test_builds = opts.list_of_targets.split(',') if opts.list_of_targets else None
    for test_build in test_spec.get_test_builds(filter_test_builds):
        platform_name = test_build.get_platform()
        gt_logger.gt_log("processing target '%s' toolchain '%s' compatible platforms..." %
                         (gt_logger.gt_bright(platform_name), gt_logger.gt_bright(test_build.get_toolchain())))

        baudrate = test_build.get_baudrate()

        ### Select MUTS to test from list of available MUTS to start testing
        mut = None
        number_of_parallel_instances = 1
        for mbed_dev in ready_mbed_devices:
            if accepted_target_ids and mbed_dev['target_id'] not in accepted_target_ids:
                continue

            if mbed_dev['platform_name'] == platform_name:
                # We will force configuration specific baudrate by adding baudrate to serial port
                # Only add baudrate decoration for serial port if it's not already there
                # Format used by mbedhtrun: 'serial_port' = '<serial_port_name>:<baudrate>'
                if not mbed_dev['serial_port'].endswith(str(baudrate)):
                    mbed_dev['serial_port'] = "%s:%d" % (mbed_dev['serial_port'], baudrate)
                mut = mbed_dev
                muts_to_test.append(mbed_dev)
                # Log on screen mbed device properties
                gt_logger.gt_log("using platform '%s' for test:"% gt_logger.gt_bright(platform_name))
                log_mbed_devices_properties(mbed_dev, verbose=opts.verbose)
                if number_of_parallel_instances < parallel_test_exec:
                    number_of_parallel_instances += 1
                else:
                    break

        # Configuration print mode:
        if opts.verbose_test_configuration_only:
            continue

        ### If we have at least one available device we can proceed
        if mut:
            target_platforms_match += 1

            build = test_build.get_name()
            build_path = test_build.get_path()

            # Demo mode: --run implementation (already added --run to mbedhtrun)
            # We want to pass file name to mbedhtrun (--run NAME  =>  -f NAME_ and run only one binary
            if opts.run_app:
                gt_logger.gt_log("running '%s' for '%s'-'%s'" % (gt_logger.gt_bright(opts.run_app),
                                                                 gt_logger.gt_bright(platform_name),
                                                                 gt_logger.gt_bright(test_build.get_toolchain())))
                disk = mut['mount_point']
                port = mut['serial_port']
                micro = mut['platform_name']
                program_cycle_s = get_platform_property(micro, "program_cycle_s")
                copy_method = opts.copy_method if opts.copy_method else 'shell'
                enum_host_tests_path = get_local_host_tests_dir(opts.enum_host_tests)

                test_platforms_match += 1
                host_test_result = run_host_test(opts.run_app,
                                                 disk,
                                                 port,
                                                 build_path,
                                                 mut['target_id'],
                                                 micro=micro,
                                                 copy_method=copy_method,
                                                 program_cycle_s=program_cycle_s,
                                                 digest_source=opts.digest_source,
                                                 json_test_cfg=opts.json_test_configuration,
                                                 run_app=opts.run_app,
                                                 enum_host_tests_path=enum_host_tests_path,
                                                 verbose=True)

                # Some error in htrun, abort test execution
                if isinstance(host_test_result, int):
                    # int(host_test_result) > 0 - Call to mbedhtrun failed
                    # int(host_test_result) < 0 - Something went wrong while executing mbedhtrun
                    return host_test_result

                # If execution was successful 'run_host_test' return tuple with results
                single_test_result, single_test_output, single_testduration, single_timeout, result_test_cases, test_cases_summary = host_test_result
                status = TEST_RESULTS.index(single_test_result) if single_test_result in TEST_RESULTS else -1
                if single_test_result != TEST_RESULT_OK:
                    test_exec_retcode += 1
                continue

            test_list = test_build.get_tests()

            filtered_ctest_test_list = create_filtered_test_list(test_list, opts.test_by_names, opts.skip_test, test_spec=test_spec)

            gt_logger.gt_log("running %d test%s for platform '%s' and toolchain '%s'"% (
                len(filtered_ctest_test_list),
                "s" if len(filtered_ctest_test_list) != 1 else "",
                gt_logger.gt_bright(platform_name),
                gt_logger.gt_bright(test_build.get_toolchain())
            ))

            # Test execution order can be shuffled (also with provided random seed)
            # for test execution reproduction.
            filtered_ctest_test_list_keys = filtered_ctest_test_list.keys()
            if opts.shuffle_test_order:
                # We want to shuffle test names randomly
                random.shuffle(filtered_ctest_test_list_keys, lambda: shuffle_random_seed)

            for test_name in filtered_ctest_test_list_keys:
                image_path = filtered_ctest_test_list[test_name].get_binary(binary_type=TestBinary.BIN_TYPE_BOOTABLE).get_path()
                if image_path is None:
                    gt_logger.gt_log_err("Failed to find test binary for test %s flash method %s" % (test_name, 'usb'))
                else:
                    test = {"test_bin": test_name, "image_path": image_path}
                    test_queue.put(test)

            number_of_threads = 0
            for mut in muts_to_test:
                # Experimental, parallel test execution
                if number_of_threads < parallel_test_exec:
                    args = (test_result_queue, test_queue, opts, mut, build, build_path, greentea_hooks)
                    t = Thread(target=run_test_thread, args=args)
                    execute_threads.append(t)
                    number_of_threads += 1

        gt_logger.gt_log_tab("use %s instance%s for testing" % (len(execute_threads), 's' if len(execute_threads) != 1 else ''))
        for t in execute_threads:
            t.daemon = True
            t.start()

        # merge partial test reports from different threads to final test report
        for t in execute_threads:
            try:
                t.join() #blocking
                test_return_data = test_result_queue.get(False)
            except Exception as e:
                # No test report generated
                gt_logger.gt_log_err("could not generate test report" + str(e))
                test_exec_retcode += -1000
                return test_exec_retcode

            test_platforms_match += test_return_data['test_platforms_match']
            test_exec_retcode += test_return_data['test_exec_retcode']
            partial_test_report = test_return_data['test_report']
            # todo: find better solution, maybe use extend
            for report_key in partial_test_report.keys():
                if report_key not in test_report:
                    test_report[report_key] = {}
                    test_report.update(partial_test_report)
                else:
                    test_report[report_key].update(partial_test_report[report_key])

        execute_threads = []

        if opts.verbose_test_configuration_only:
            print
            print "Example: execute 'mbedgt --target=TARGET_NAME' to start testing for TARGET_NAME target"
            return (0)

        gt_logger.gt_log("all tests finished!")

    # We will execute post test hooks on tests
    for build_name in test_report:
        test_name_list = []    # All test case names for particular yotta target
        for test_name in test_report[build_name]:
            test = test_report[build_name][test_name]
            # Test was successful
            if test['single_test_result'] in [TEST_RESULT_OK, TEST_RESULT_FAIL]:
                test_name_list.append(test_name)
                # Call hook executed for each test, just after all tests are finished
                if greentea_hooks:
                    # We can execute this test hook just after all tests are finished ('hook_post_test_end')
                    format = {
                        "test_name": test_name,
                        "test_bin_name": test['test_bin_name'],
                        "image_path": test['image_path'],
                        "build_path": test['build_path'],
                        "build_path_abs": test['build_path_abs'],
                    }
                    greentea_hooks.run_hook_ext('hook_post_test_end', format)
        if greentea_hooks:
            build = test_spec.get_test_build(build_name)
            assert build is not None, "Failed to find build info for build %s" % build_name

            # Call hook executed for each yotta target, just after all tests are finished
            build_path = build.get_path()
            build_path_abs = os.path.abspath(build_path)
            # We can execute this test hook just after all tests are finished ('hook_post_test_end')
            format = {
                "build_path": build_path,
                "build_path_abs": build_path_abs,
                "test_name_list": test_name_list,
            }
            greentea_hooks.run_hook_ext('hook_post_all_test_end', format)

    # This tool is designed to work in CI
    # We want to return success codes based on tool actions,
    # only if testes were executed and all passed we want to
    # return 0 (success)
    if not opts.only_build_tests:
        # Prints shuffle seed
        gt_logger.gt_log("shuffle seed: %.*f"% (SHUFFLE_SEED_ROUND, shuffle_random_seed))

        # Reports (to file)
        if opts.report_junit_file_name:
            gt_logger.gt_log("exporting to JUnit file '%s'..."% gt_logger.gt_bright(opts.report_junit_file_name))
            # This test specification will be used by JUnit exporter to populate TestSuite.properties (useful meta-data for Viewer)
            junit_test_spec = test_spec if opts.test_spec else None
            junit_report = exporter_testcase_junit(test_report, test_suite_properties = get_test_suite_properties(test_spec=junit_test_spec))
            with open(opts.report_junit_file_name, 'w') as f:
                f.write(junit_report)
        if opts.report_text_file_name:
            gt_logger.gt_log("exporting to text '%s'..."% gt_logger.gt_bright(opts.report_text_file_name))

            text_report, text_results = exporter_text(test_report)
            text_testcase_report, text_testcase_results = exporter_testcase_text(test_report)
            with open(opts.report_text_file_name, 'w') as f:
                f.write('\n'.join([text_report, text_results, text_testcase_report, text_testcase_results]))

        # Reports (to console)
        if opts.report_json:
            # We will not print summary and json report together
            gt_logger.gt_log("json test report:")
            print exporter_json(test_report)
        else:
            # Final summary
            if test_report:
                # Test suite report
                gt_logger.gt_log("test suite report:")
                text_report, text_results = exporter_text(test_report)
                print text_report
                gt_logger.gt_log("test suite results: " + text_results)
                # test case detailed report
                gt_logger.gt_log("test case report:")
                text_testcase_report, text_testcase_results = exporter_testcase_text(test_report)
                print text_testcase_report
                gt_logger.gt_log("test case results: " + text_testcase_results)

        # This flag guards 'build only' so we expect only yotta errors
        if test_platforms_match == 0:
            # No tests were executed
            gt_logger.gt_log_warn("no platform/target matching tests were found!")
            test_exec_retcode += -10
        if target_platforms_match == 0:
            # No platforms were tested
            gt_logger.gt_log_warn("no target matching platforms were found!")
            test_exec_retcode += -100

    return (test_exec_retcode)
