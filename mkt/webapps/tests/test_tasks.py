import datetime
import json

from django.conf import settings
from django.core.files.storage import default_storage as storage
from django.core.management import call_command

import mock
from nose.tools import eq_

import amo
import amo.tests
from files.models import File
from mkt.developers.models import ActivityLog
from mkt.reviewers.models import RereviewQueue
from mkt.webapps.models import Webapp
from mkt.webapps.tasks import update_manifests
from users.models import UserProfile
from versions.models import Version

original = {
        "version": "0.1",
        "name": "MozillaBall",
        "description": "Exciting Open Web development action!",
        "icons": {
            "16": "http://test.com/icon-16.png",
            "48": "http://test.com/icon-48.png",
            "128": "http://test.com/icon-128.png"
        },
        "installs_allowed_from": [
            "*",
        ],
    }


new = {
        "version": "1.0",
        "name": "MozillaBall",
        "description": "Exciting Open Web development action!",
        "icons": {
            "16": "http://test.com/icon-16.png",
            "48": "http://test.com/icon-48.png",
            "128": "http://test.com/icon-128.png"
        },
        "installs_allowed_from": [
            "*",
        ],
    }


ohash = 'sha256:fc11fba25f251d64343a7e8da4dfd812a57a121e61eb53c78c567536ab39b10d'
nhash = 'sha256:409fbe87dca5a4a7937e3dea27b69cb3a3d68caf39151585aef0c7ab46d8ee1e'


class TestUpdateManifest(amo.tests.TestCase):
    fixtures = ('base/platforms',)

    def setUp(self):
        UserProfile.objects.create(id=settings.TASK_USER_ID)
        self.addon = Webapp.objects.create(type=amo.ADDON_WEBAPP)
        self.version = Version.objects.create(addon=self.addon)
        self.version.update(
            created=datetime.datetime.now() - datetime.timedelta(days=1),
            modified=datetime.datetime.now() - datetime.timedelta(days=1))
        self.file = File.objects.create(version=self.version, platform_id=1,
                                        filename='manifest.webapp', hash=ohash,
                                        status=amo.STATUS_PUBLIC)
        with storage.open(self.file.file_path, 'w') as fh:
            fh.write(json.dumps(original))

        # This is the hash to set the get_content_hash to, for showing
        # that the webapp has been updated.
        self._hash = nhash
        self._data = json.dumps(new)

        urlopen_patch = mock.patch('urllib2.urlopen')
        self.urlopen_mock = urlopen_patch.start()
        self.addCleanup(urlopen_patch.stop)

        response_mock = mock.Mock()
        response_mock.read.return_value = json.dumps(new)
        response_mock.headers = {
            'Content-Type': 'application/x-web-app-manifest+json'}
        self.urlopen_mock.return_value = response_mock

    @mock.patch('mkt.webapps.tasks._get_content_hash')
    def _run(self, _get_content_hash):
        # Will run the task and will act depending upon how you've set hash.
        _get_content_hash.return_value = self._hash
        update_manifests(ids=(self.addon.pk,))

    def test_new_version(self):
        eq_(self.addon.versions.count(), 1)
        old_version = self.addon.current_version
        old_file = self.addon.get_latest_file
        self._run()

        # Test that our new version looks good
        new = Webapp.objects.get(pk=self.addon.pk)
        eq_(new.versions.count(), 2)
        assert new.current_version != old_version, 'Version not updated'
        assert new.get_latest_file() != old_file, 'File not updated'

    def test_new_version_multiple(self):
        self._run()
        self._data = self._data.replace('1.0', '1.1')
        self._hash = 'foo'
        self._run()

        new = Webapp.objects.get(pk=self.addon.pk)
        eq_(new.versions.count(), 3)

    def test_not_log(self):
        self._hash = ohash
        self._run()
        eq_(ActivityLog.objects.for_apps(self.addon).count(), 0)

    def test_log(self):
        self._run()
        eq_(ActivityLog.objects.for_apps(self.addon).count(), 1)

    @mock.patch('mkt.webapps.tasks.update_manifests')
    def test_ignore_not_webapp(self, update_manifests):
        self.addon.update(type=amo.ADDON_EXTENSION)
        call_command('process_addons', task='update_manifests')
        assert not update_manifests.call_args

    @mock.patch('mkt.webapps.tasks.update_manifests')
    def test_ignore_disabled(self, update_manifests):
        self.addon.update(status=amo.STATUS_DISABLED)
        call_command('process_addons', task='update_manifests')
        assert not update_manifests.call_args

    @mock.patch('mkt.webapps.tasks.update_manifests')
    def test_get_webapp(self, update_manifests):
        call_command('process_addons', task='update_manifests')
        assert not update_manifests.call_args

    @mock.patch('mkt.webapps.tasks._open_manifest')
    def test_manifest_name_change_rereview(self, open_manifest):
        # Mock original manifest file lookup.
        open_manifest.return_value = original
        # Mock new manifest with name change.
        n = new.copy()
        n['name'] = 'Mozilla Ball Ultimate Edition'
        response_mock = mock.Mock()
        response_mock.read.return_value = json.dumps(n)
        response_mock.headers = {
            'Content-Type': 'application/x-web-app-manifest+json'}
        self.urlopen_mock.return_value = response_mock

        eq_(RereviewQueue.objects.count(), 0)
        self._run()
        eq_(RereviewQueue.objects.count(), 1)
        # 2 logs: 1 for manifest update, 1 for re-review trigger.
        eq_(ActivityLog.objects.for_apps(self.addon).count(), 2)

    @mock.patch.object(settings, 'SITE_URL', 'http://test')
    @mock.patch('mkt.webapps.tasks._open_manifest')
    def test_validation_error_logs(self, open_manifest):
        # Mock original manifest file lookup.
        open_manifest.return_value = original
        # Mock new manifest with name change.
        n = new.copy()
        n['locale'] = 'en-US'
        response_mock = mock.Mock()
        response_mock.read.return_value = json.dumps(n)
        response_mock.headers = {
            'Content-Type': 'application/x-web-app-manifest+json'}
        self.urlopen_mock.return_value = response_mock

        eq_(RereviewQueue.objects.count(), 0)
        self._run()
        eq_(RereviewQueue.objects.count(), 1)
        assert 'http://test/en-US/developers/upload' in ''.join(
            [a._details for a in ActivityLog.objects.for_apps(self.addon)])
        eq_(ActivityLog.objects.for_apps(self.addon).count(), 1)

        # Test we don't add app to re-review queue twice.
        self._run()
        eq_(RereviewQueue.objects.count(), 1)
