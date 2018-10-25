##############################################################################
#
# Copyright (c) 2009-2010 Zope Corporation and Contributors.
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

import cPickle
import cStringIO
import logging
import types
import sys

from ZODB.broken import find_global, Broken, rebuild
from zodbupdate import utils

logger = logging.getLogger('zodbupdate')
known_broken_modules = {}


def is_broken(symb):
    """Return true if the given symbol is broken.
    """
    return isinstance(symb, types.TypeType) and Broken in symb.__mro__


def create_broken_module_for(symb):
    """If your pickle refer a broken class (not an instance of it, a
       reference to the class symbol itself) you have no choice than
       having this module available in the same symbol and with the
       same name, otherwise repickling doesn't work (as both pickle
       and cPikle __import__ the module, and verify the class symbol
       is the same than the one provided).
    """
    parts = symb.__module__.split('.')
    previous = None
    for fullname, name in reversed(
        [('.'.join(parts[0:p+1]), parts[p]) for p in range(1, len(parts))]):
        if fullname not in sys.modules:
            if fullname not in known_broken_modules:
                module = types.ModuleType(fullname)
                module.__name__ = name
                module.__file__ = '<broken module to pickle class reference>'
                module.__path__ = []
                known_broken_modules[fullname] = module
            else:
                if previous:
                    module = known_broken_modules[fullname]
                    setattr(module, *previous)
                break
            if previous:
                setattr(module, *previous)
            previous = (name, module)
        else:
            if previous:
                setattr(sys.modules[fullname], *previous)
                break
    if symb.__module__ in known_broken_modules:
        setattr(known_broken_modules[symb.__module__], symb.__name__, symb)
    elif symb.__module__ in sys.modules:
        setattr(sys.modules[symb.__module__], symb.__name__, symb)


class BrokenModuleFinder(object):
    """This broken module finder works with create_broken_module_for.
    """

    def load_module(self, fullname):
        module = known_broken_modules[fullname]
        if fullname not in sys.modules:
            sys.modules[fullname] = module
        module.__loader__ = self
        return module

    def find_module(self, fullname, path=None):
        if fullname in known_broken_modules:
            return self
        return None


sys.meta_path.append(BrokenModuleFinder())


class NullIterator(object):
    """An empty iterator that doesn't gives any result.
    """

    def __iter__(self):
        return self

    def next(self):
        raise StopIteration


class IterableClass(type):

    def __iter__(cls):
        """Define a empty iterator to fix unpickling of missing
        Interfaces that have been used to do alsoProvides on a another
        pickled object.
        """
        return NullIterator()


class ZODBBroken(Broken):
    """Extend ZODB Broken to work with broken objects that doesn't
    have any __Broken_newargs__ sets (which happens if their __new__
    method is not called).
    """
    __metaclass__ = IterableClass

    def __reduce__(self):
        """We pickle broken objects in hope of being able to fix them later.
        """
        return (rebuild,
                ((self.__class__.__module__, self.__class__.__name__)
                 + getattr(self, '__Broken_newargs__', ())),
                self.__Broken_state__)


class ZODBReference(object):
    """Class to remenber reference we don't want to touch.
    """

    def __init__(self, ref):
        self.ref = ref


class ObjectRenamer(object):
    """This load and save a ZODB record, modifying all references to
    renamed class according the given renaming rules:

    - in global symbols contained in the record,

    - in persistent reference information,

    - in class information (first pickle of the record).
    """

    def __init__(self, changes, class_by_oid={}, pickler_name='C'):
        self.__added = dict()
        self.__changes = dict()
        for old, new in changes.iteritems():
            self.__changes[tuple(old.split(' '))] = tuple(new.split(' '))
        self.__class_by_oid = class_by_oid
        self.__changed = False
        self.__pickler_name = pickler_name

    def __update_symb(self, symb_info, oid=None):
        """This method look in a klass or symbol have been renamed or
        not. If the symbol have not been renamed explicitly, it's
        loaded and its location is checked to see if it have moved as
        well.
        """
        if oid in self.__class_by_oid:
            self.__changed = True
            return self.__class_by_oid[oid]
        elif symb_info in self.__changes:
            self.__changed = True
            return self.__changes[symb_info]
        else:
            symb = find_global(*symb_info, Broken=ZODBBroken)
            if is_broken(symb):
                logger.warning(
                    u'Warning: Missing factory for %s' % u' '.join(symb_info))
                create_broken_module_for(symb)
            elif hasattr(symb, '__name__') and hasattr(symb, '__module__'):
                new_symb_info = (symb.__module__, symb.__name__)
                if new_symb_info != symb_info:
                    logger.info(
                        u'New implicit rule detected %s to %s' %
                        (u' '.join(symb_info), u' '.join(new_symb_info)))
                    self.__changes[symb_info] = new_symb_info
                    self.__added[symb_info] = new_symb_info
                    self.__changed = True
                    return new_symb_info
        return symb_info

    def __find_global(self, *klass_info):
        """Find a class with the given name, looking for a renaming
        rule first.

        Using ZODB find_global let us manage missing classes.
        """
        return find_global(*self.__update_symb(klass_info), Broken=ZODBBroken)

    def __persistent_load(self, reference):
        """Load a persistent reference. The reference might changed
        according a renaming rules. We give back a special object to
        represent that reference, and not the real object designated
        by the reference.
        """
        if isinstance(reference, tuple):
            oid, klass_info = reference
            if oid in self.__class_by_oid:
                self.__changed = True
                klass_info = find_global(*self.__class_by_oid[oid], Broken=ZODBBroken)
            elif isinstance(klass_info, tuple):
                klass_info = self.__update_symb(klass_info, oid=oid)
            return ZODBReference((oid, klass_info))
        if isinstance(reference, list):
            mode, information = reference
            if mode == 'm':
                database_name, oid, klass_info = information
                if oid in self.__class_by_oid:
                    self.__changed = True
                    klass_info = find_global(*self.__class_by_oid[oid], Broken=ZODBBroken)
                elif isinstance(klass_info, tuple):
                    klass_info = self.__update_symb(klass_info, oid=oid)
                return ZODBReference(['m', (database_name, oid, klass_info)])
        return ZODBReference(reference)

    def __unpickler(self, input_file):
        """Create an unpickler with our custom global symbol loader
        and reference resolver.
        """
        return utils.UNPICKLERS[self.__pickler_name](
            input_file, self.__persistent_load, self.__find_global)

    def __persistent_id(self, obj):
        """Save the given object as a reference only if it was a
        reference before. We re-use the same information.
        """
        if not isinstance(obj, ZODBReference):
            return None
        return obj.ref

    def __pickler(self, output_file):
        """Create a pickler able to save to the given file, objects we
        loaded while paying attention to any reference we loaded.
        """
        pickler = cPickle.Pickler(output_file, 1)
        pickler.persistent_id = self.__persistent_id
        return pickler

    def __update_class_meta(self, class_meta, oid):
        """Update class information, which can contain information
        about a renamed class.
        """
        if isinstance(class_meta, tuple):
            symb, args = class_meta
            if is_broken(symb):
                symb_info = (symb.__module__, symb.__name__)
                logger.warning(
                    u'Warning: Missing factory for %s' % u' '.join(symb_info))
                return (symb_info, args)
            elif isinstance(symb, tuple):
                return self.__update_symb(symb, oid=oid), args
        return class_meta

    def rename(self, input_file, oid):
        """Take a ZODB record (as a file object) as input. We load it,
        replace any reference to renamed class we know of. If any
        modification are done, we save the record again and return it,
        return None otherwise.
        """
        self.__changed = False

        unpickler = self.__unpickler(input_file)
        class_meta = unpickler.load()
        data = unpickler.load()

        class_meta = self.__update_class_meta(class_meta, oid)

        if not (self.__changed or
                (hasattr(unpickler, 'need_repickle') and
                 unpickler.need_repickle())):
            return None

        output_file = cStringIO.StringIO()
        pickler = self.__pickler(output_file)
        try:
            pickler.dump(class_meta)
            pickler.dump(data)
        except cPickle.PicklingError, error:
            logger.error('Error: cannot pickling modified record: %s' % error)
            # Could not pickle that record, skip it.
            return None

        output_file.truncate()
        return output_file

    def get_found_implicit_rules(self):
        result = {}
        for old, new in self.__added.items():
            result[' '.join(old)] = ' '.join(new)
        return result
