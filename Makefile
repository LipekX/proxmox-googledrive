.PHONY: all test deb clean

all: deb

test:
	python3 -m unittest discover -s tests -p "test_daemon.py" -v
	perl -I tests/perl_mocks -I perl -c perl/PVE/Storage/Custom/GDrivePlugin.pm
	perl -I tests/perl_mocks -I perl -c perl/PVE/API2/GDrive.pm
	perl -I tests/perl_mocks -I perl -c scripts/vzdump-gdrive-hook.pl
	perl tests/test_perl.pl
	node --check relay/worker.js
	node --check ui/pve-gdrive.js
	node --test tests/test_webui.mjs
	bash -n ui/overlay-hook.sh
	bash -n local_install.sh
	bash -n revert.sh
	bash -n scripts/install.sh
	bash -n debian/postinst
	bash -n debian/prerm
	python3 -m py_compile scripts/build-deb.py

deb:
	python3 scripts/build-deb.py

clean:
	rm -rf dist build debian/pve-storage-gdrive debian/.debhelper debian/files debian/*.substvars debian/*.debhelper.log
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.py[cod]' -type f -delete
