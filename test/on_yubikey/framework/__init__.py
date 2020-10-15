from __future__ import print_function
import click
import functools
import os
import sys
import test.util
import unittest
import time

from yubikit.core import USB_INTERFACE
from ykman.device import list_all_devices, connect_to_device, get_connection_types


_skip = True

_test_serials_env = os.environ.get("DESTRUCTIVE_TEST_YUBIKEY_SERIALS")
_test_serials = set()
_serials_present = set()
_device_info = {}
_no_prompt = os.environ.get("DESTRUCTIVE_TEST_DO_NOT_PROMPT") == "TRUE"
_versions = {}

if _test_serials_env is not None:
    start_time = time.time()
    print("Initiating device discovery...")

    _test_serials = set(int(s) for s in _test_serials_env.split(","))

    for pid, info in list_all_devices():
        print("{:.3f} {}".format(time.time() - start_time, info.serial))
        _serials_present.add(info.serial)
        _device_info[info.serial] = info
        _versions[info.serial] = info.version

    _unwanted_serials = _serials_present.difference(_test_serials)

    if len(_unwanted_serials) != 0:
        print(
            "Encountered YubiKeys not listed in serial numbers to be used "
            "for the test: {}".format(_unwanted_serials),
            file=sys.stderr,
        )
        sys.exit(1)

    if _serials_present != _test_serials:
        print(
            "Test YubiKeys missing: {}".format(
                _test_serials.difference(_serials_present)
            ),
            file=sys.stderr,
        )
        sys.exit(1)

    _skip = False

    if not _no_prompt:
        click.confirm(
            "Run integration tests? This will erase data on the YubiKeys"
            " with serial numbers: {}. Make sure these are all keys used for"
            " development.".format(_serials_present),
            abort=True,
        )

    end_time = time.time()
    print("Device discovery finished in {:.3f} s".format(end_time - start_time))


def exactly_one_yubikey_present():
    return len(_serials_present) == 1


def partial_with_retry(func, *partial_args, **partial_kwargs):
    """
    Like functools.partial, but adds a `retry_count` parameter to the wrapped
    function.

    If the wrapped function raises a non-exit exception or an `OSError`, then
    the returned function waits for 0.5 seconds and then retries the wrapped
    function call with the same arguments. This is done no more than
    `retry_count` times, after which the exception is re-raised.

    The `retry_count` argument is not passed to the wrapped function.
    """

    default_retry_count = partial_kwargs.pop("default_retry_count", 0)

    @functools.wraps(func)
    def wrap_func(*args, **kwargs):
        retry_count = kwargs.pop("retry_count", default_retry_count)
        for k, v in partial_kwargs.items():
            kwargs.setdefault(k, v)
        try:
            return func(*(partial_args + args), **kwargs)
        except Exception or OSError:
            if retry_count > 0:
                time.sleep(0.5)
                return wrap_func(*args, retry_count=retry_count - 1, **kwargs)
            raise

    return wrap_func


def _specialize_ykman_cli(serial, _interfaces):
    """
    Creates a specialized version of ykman_cli preset with the serial number of
    the given device.
    """
    f = functools.partial(test.util.ykman_cli, "--device", serial)
    f.with_bytes_output = partial_with_retry(
        test.util.ykman_cli_bytes, "--device", serial, default_retry_count=1
    )
    return f


def _specialize_open_device(serial, interface):
    """
    Creates a specialized version of open_device which will open the given
    device using the given interface(s).
    """
    assert (
        interface.name
    ), "_specialize_open_device accepts only one interface at a time."

    return partial_with_retry(
        connect_to_device,
        serial=serial,
        connection_types=get_connection_types(interface),
        default_retry_count=1,
    )


def _make_skipped_original_test_cases(create_test_classes):
    for test_class in create_test_classes(None):
        yield unittest.skip("No YubiKey available for test")(test_class)


def _device_satisfies_test_conditions(info, test_method):
    if "_yubikey_conditions" in dir(test_method):
        conditions = getattr(test_method, "_yubikey_conditions")
        return all(cond(info) for cond in conditions)
    else:
        return True


def _delete_inapplicable_test_methods(info, test_class):
    for method_name in _get_test_method_names(test_class):
        method = getattr(test_class, method_name)
        if not _device_satisfies_test_conditions(info, method):
            delattr(test_class, method_name)
    return test_class


def _add_suffix_to_class_name(interface, info, test_class):
    setattr(test_class, "_original_test_name", test_class.__qualname__)
    interface_part = (
        "_{}".format(interface.name) if isinstance(interface, USB_INTERFACE) else ""
    )
    fw_version = ".".join(str(v) for v in info.version)
    test_class.__qualname__ = "{}{}_{}_{}".format(
        test_class._original_test_name, interface_part, fw_version, info.serial
    )
    return test_class


def _create_test_classes_for_device(
    interface, info, create_test_classes, create_test_class_context
):
    """
    Create test classes for the given device via the given interface.

    A suffix with the interface, device firmware version and device serial
    number is added to the name of each test class returned by
    create_test_classes.

    Each test class is filtered to contain only the tests applicable to the
    device for that test class.

    :param interface: the ykman.util.USB_INTERFACE to use when opening the device
    :param dev: the ykman.device.YubiKey whose serial number to use when
            opening the device.
    :param create_test_classes: the additional_tests function that was
            decorated with @device_test_suite or @cli_test_suite.
    :param create_test_class_context: a function which, given a
            ykman.device.Yubikey and a ykman.util.USB_INTERFACE, returns a
            specialized open_device or ykman_cli function for that device and
            interface.
    """
    context = create_test_class_context(info.serial, interface)
    for test_class in create_test_classes(context):
        _delete_inapplicable_test_methods(info, test_class)
        _add_suffix_to_class_name(interface, info, test_class)
        yield test_class


def _get_test_method_names(test_class):
    return set(
        attr_name for attr_name in dir(test_class) if attr_name.startswith("test")
    )


def _multiply_test_classes_by_devices(
    interfaces_and_serials, create_test_classes, create_test_class_context,
):
    """
    Instantiate device-specific versions of test classes for each combination
    of the given interfaces and the available devices.

    Each test class returned by create_test_classes is instantiated for each
    combination of interface and device.

    :param interfaces_and_serials: a sequence of (ykman.util.USB_INTERFACE,
            ykman.device.YubiKey) pairs, for each of which to instantiate each
            test.
    :param create_test_classes: the additional_tests function that was
            decorated with @device_test_suite or @cli_test_suite.
    :param create_test_class_context: a function which, given a
            ykman.device.Yubikey and a ykman.util.USB_INTERFACE, returns a
            specialized open_device or ykman_cli function for that device and
            interface.
    :returns: an iterable of instantiated tests and a dict with original test
            class names mapped to sets of test method names that were
            instantiated.
    """

    tests = []
    covered_test_names = {}

    for (interface, serial) in interfaces_and_serials:
        info = _device_info[serial]
        for test_class in _create_test_classes_for_device(
            interface, info, create_test_classes, create_test_class_context
        ):
            orig_name = test_class._original_test_name
            test_names = _get_test_method_names(test_class)
            covered_test_names[orig_name] = covered_test_names.get(
                orig_name, set()
            ).union(test_names)
            for test_method_name in test_names:
                tests.append(test_class(test_method_name))

    return tests, covered_test_names


def _make_skips_for_uncovered_tests(create_test_classes, covered_test_names):
    for original_test_class in _make_skipped_original_test_cases(create_test_classes):
        original_test_names = _get_test_method_names(original_test_class)
        uncovered_test_names = original_test_names.difference(
            covered_test_names.get(original_test_class.__qualname__, set())
        )

        for uncovered_test_name in uncovered_test_names:
            yield original_test_class(uncovered_test_name)


def _make_test_suite_decorator(interfaces_and_serials, create_test_class_context):
    """
    Create a decorator that will instantiate device-specific versions of the
    test classes returned by the decorated function.

    :param interfaces_and_serials: a sequence of (ykman.util.USB_INTERFACE,
            ykman.device.YubiKey) pairs, for each of which to instantiate each
            test.
    :param create_test_class_context: a function which, given a
            ykman.device.Yubikey and a ykman.util.USB_INTERFACE, returns a
            specialized open_device or ykman_cli function for that device and
            interface.
    :returns: a decorator that transforms an additional_tests function into the
            format expected by unittest test discovery.
    """

    def decorate(create_test_classes):
        def additional_tests():
            if sys.version_info < (3, 0):
                # Workaround since framework crashes in py2
                # This if statement can be deleted when py2 support is dropped
                return unittest.TestSuite()

            start_time = time.time()
            print(
                "Starting test instantiation: {} ...".format(
                    create_test_classes.__module__
                )
            )
            (tests, covered_test_names) = _multiply_test_classes_by_devices(
                interfaces_and_serials, create_test_classes, create_test_class_context
            )

            skipped_tests = _make_skips_for_uncovered_tests(
                create_test_classes, covered_test_names
            )

            suite = unittest.TestSuite()
            suite.addTests(tests)
            suite.addTests(skipped_tests)

            end_time = time.time()
            print(
                "Test instantiation completed in {:.3f} s".format(end_time - start_time)
            )

            return suite

        return additional_tests

    return decorate


def device_test_suite(interfaces):
    """
    Transform an additional_tests function into the format expected by unittest
    test discovery.

    The decorated function must take one parameter, which will receive a
    specialized ykman.descriptor.open_device function as an argument. This
    open_device function opens a specific YubiKey device via a specific
    interface, and can be used as if that YubiKey is the only one connected.
    The tests defined in the decorated function should use this argument to
    open a YubiKey.

    Each test class is instantiated once per device and interface, with an
    open_device argument function specialized for that combination of device
    and interface.

    The test methods in the annotated function can be decorated with conditions
    from the yubikey_conditions module. These condition decorators will ensure
    that the decorated test is not run with YubiKey devices that do not match
    the conditions.

    :param interfaces: the ykman.util.USB_INTERFACEs to use to open YubiKey devices.
    :returns: a decorator that transforms an additional_tests function into the
            format expected by unittest test discovery.
    """
    if not (isinstance(interfaces, USB_INTERFACE) or isinstance(interfaces, int)):
        raise ValueError(
            "Argument to @device_test_suite must be a USB_INTERFACE value."
        )  # noqa: E501

    return _make_test_suite_decorator(
        ((t, s) for t in USB_INTERFACE if t & interfaces for s in _test_serials or []),
        _specialize_open_device,
    )


def cli_test_suite(additional_tests):
    """
    Transform an additional_tests function into the format expected by unittest
    test discovery.

    The decorated function must take one parameter, which will receive a
    specialized test.util.ykman_cli function as an argument. This ykman_cli
    function has the --device option set, so it uses a specific YubiKey device,
    and can be used as if that YubiKey is the only one connected. The tests
    defined in the decorated function should use this argument to run the ykman
    CLI.

    The test methods in the annotated function can be decorated with conditions
    from the yubikey_conditions module. These condition decorators will ensure
    that the decorated test is not run with YubiKey devices that do not match
    the conditions.

    :param additional_tests: The decorated function
    :returns: the argument function transformed into the format expected by
            unittest test discovery.
    """
    return _make_test_suite_decorator(
        ((sum(USB_INTERFACE), s) for s in _test_serials or []), _specialize_ykman_cli
    )(additional_tests)


destructive_tests_not_activated = (_skip, "DESTRUCTIVE_TEST_YUBIKEY_SERIALS == None")


@unittest.skipIf(*destructive_tests_not_activated)
class DestructiveYubikeyTestCase(unittest.TestCase):
    pass
