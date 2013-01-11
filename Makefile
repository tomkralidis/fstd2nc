SUBDIRS = 
LIBS = rpn bmf geophy
COMMON = io compress
include rules.mk

install:
	$(MAKE)
	python setup.py build
	python setup.py install --prefix=$(DESTDIR)/usr/local
	./cp_libs.sh
	./cp_bins.sh
