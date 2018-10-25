##############################################################################
#
# Copyright (c) 2009 Zope Corporation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################

import ZODB.FileStorage
import ZODB.config
import ZODB.serialize
import ZODB.utils
import json
import logging
import optparse
import pkg_resources
import pprint
import sys
import time
import zodbupdate.update
import zodbupdate.utils


parser = optparse.OptionParser(
    description="Updates all references to classes to their canonical location.")
parser.add_option(
    "-f", "--file",
    help="load FileStorage")
parser.add_option(
    "-c", "--config",
    help="load storage from config file")
parser.add_option(
    "-n", "--dry-run", action="store_true",
    help="perform a trial run with no changes made")
parser.add_option(
    "-s", "--save-renames",
    help="save automatically determined rename rules to file")
parser.add_option(
    "-q", "--quiet", action="store_true",
    help="suppress non-error messages")
parser.add_option(
    "-v", "--verbose", action="store_true",
    help="more verbose output")
parser.add_option(
    "-o", "--oid",
    help="start with provided oid in hex format, ex: 0xaa1203")
parser.add_option(
    "-d", "--debug", action="store_true",
    help="post mortem pdb on failure")
parser.add_option(
    "-p", "--pickler", default="C",
    help="chooser unpickler implementation C or Python (default C)")
parser.add_option(
    "--pack", action="store_true", dest="pack",
    help="pack the storage when done. use in conjunction of -c if you have blobs storage")
parser.add_option(
    "-k", "--known-classes",
    help="file containing a mapping from oids to module/class tuples in JSON format")


class DuplicateFilter(object):

    def __init__(self):
        self.seen = set()

    def filter(self, record):
        if record.msg in self.seen:
            return False
        self.seen.add(record.msg)
        return True

duplicate_filter = DuplicateFilter()


def main():
    options, args = parser.parse_args()

    if options.pickler not in zodbupdate.utils.UNPICKLERS:
        raise SystemExit(u'Invalid pickler chosen. Available picklers: %s' %
                         ', '.join(zodbupdate.utils.UNPICKLERS))

    if options.quiet:
        level = logging.ERROR
    elif options.verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logging.getLogger().addHandler(logging.StreamHandler())
    logging.getLogger().setLevel(level)
    logging.getLogger('zodbupdate').addFilter(duplicate_filter)

    if options.file and options.config:
        raise SystemExit(
            u'Exactly one of --file or --config must be given.')

    if options.file:
        storage = ZODB.FileStorage.FileStorage(options.file)
    elif options.config:
        storage = ZODB.config.storageFromURL(options.config)
    else:
        raise SystemExit(
            u'Exactly one of --file or --config must be given.')

    start_at = '0x00'
    if options.oid:
        start_at = options.oid

    class_by_oid = {}
    if options.known_classes:
        classes_file = open(options.known_classes, 'r')
        class_by_oid = {ZODB.utils.repr_to_oid(key): value
                        for key, value in json.load(classes_file).items()}
        classes_file.close()

    rename_rules = {}
    for entry_point in pkg_resources.iter_entry_points('zodbupdate'):
        rules = entry_point.load()
        rename_rules.update(rules)
        logging.info(u'Loaded %s rules from %s:%s' %
                      (len(rules), entry_point.module_name, entry_point.name))

    updater = zodbupdate.update.Updater(
        storage,
        dry=options.dry_run,
        renames=rename_rules,
        class_by_oid=class_by_oid,
        start_at=start_at,
        debug=options.debug,
        pickler_name=options.pickler)

    try:
        updater()
    except Exception, e:
        logging.debug(u'An error occured', exc_info=True)
        logging.error(u'Stopped processing, due to: %s' % e)
        raise SystemExit()

    implicit_renames = updater.processor.get_found_implicit_rules()
    if implicit_renames:
        print 'Found new rules:'
        print pprint.pformat(implicit_renames)
    if options.save_renames:
        print 'Saving rules into %s' % options.save_renames
        rename_rules.update(implicit_renames)
        f = open(options.save_renames, 'w')
        f.write('renames = %s' % pprint.pformat(rename_rules))
        f.close()
    if options.pack:
        print 'Packing storage ...'
        storage.pack(time.time(), ZODB.serialize.referencesf)
    storage.close()

