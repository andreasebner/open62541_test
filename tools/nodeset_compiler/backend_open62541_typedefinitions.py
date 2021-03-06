from __future__ import print_function
import re
import itertools
import sys
import time
import getpass
import platform
from collections import OrderedDict

if sys.version_info[0] >= 3:
    from nodeset_compiler.type_parser import BuiltinType, EnumerationType, OpaqueType, StructType
else:
    from type_parser import BuiltinType, EnumerationType, OpaqueType, StructType

# Some types can be memcpy'd off the binary stream. That's especially important
# for arrays. But we need to check if they contain padding and whether the
# endianness is correct. This dict gives the C-statement that must be true for the
# type to be overlayable. Parsed types are added if they apply.
builtin_overlayable = {"Boolean": "true",
                       "SByte": "true", "Byte": "true",
                       "Int16": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "UInt16": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "Int32": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "UInt32": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "Int64": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "UInt64": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "Float": "UA_BINARY_OVERLAYABLE_FLOAT",
                       "Double": "UA_BINARY_OVERLAYABLE_FLOAT",
                       "DateTime": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "StatusCode": "UA_BINARY_OVERLAYABLE_INTEGER",
                       "Guid": "(UA_BINARY_OVERLAYABLE_INTEGER && " +
                               "offsetof(UA_Guid, data2) == sizeof(UA_UInt32) && " +
                               "offsetof(UA_Guid, data3) == (sizeof(UA_UInt16) + sizeof(UA_UInt32)) && " +
                               "offsetof(UA_Guid, data4) == (2*sizeof(UA_UInt32)))"}

whitelistFuncAttrWarnUnusedResult = []  # for instances [ "String", "ByteString", "LocalizedText" ]


# Escape C strings:
def makeCLiteral(value):
    return re.sub(r'(?<!\\)"', r'\\"', value.replace('\\', r'\\\\').replace('\n', r'\\n').replace('\r', r''))


# Strip invalid characters to create valid C identifiers (variable names etc):
def makeCIdentifier(value):
    return re.sub(r'[^\w]', '', value)


def getNodeidTypeAndId(nodeId):
    if '=' not in nodeId:
        return "UA_NODEIDTYPE_NUMERIC, {{{0}LU}}".format(nodeId)
    if nodeId.startswith("i="):
        return "UA_NODEIDTYPE_NUMERIC, {{{0}LU}}".format(nodeId[2:])
    if nodeId.startswith("s="):
        strId = nodeId[2:]
        return "UA_NODEIDTYPE_STRING, {{ .string = UA_STRING_STATIC(\"{id}\") }}".format(id=strId.replace("\"", "\\\""))

class CGenerator(object):
    def __init__(self, parser, inname, outfile, is_internal_types):
        self.parser = parser
        self.inname = inname
        self.outfile = outfile
        self.is_internal_types = is_internal_types
        self.filtered_types = None
        self.fh = None
        self.ff = None
        self.fc = None
        self.fe = None

    @staticmethod
    def get_type_index(datatype):
        if isinstance(datatype,  BuiltinType):
            return makeCIdentifier("UA_TYPES_" + datatype.name.upper())
        if isinstance(datatype, EnumerationType):
            return datatype.strTypeIndex;

        if datatype.name is not None:
            return "UA_" + makeCIdentifier(datatype.outname.upper() + "_" + datatype.name.upper())
        return makeCIdentifier(datatype.outname.upper())

    @staticmethod
    def get_type_kind(datatype):
        if isinstance(datatype, BuiltinType):
            return "UA_DATATYPEKIND_" + datatype.name.upper()
        if isinstance(datatype, EnumerationType):
            return datatype.strTypeKind
        if isinstance(datatype, OpaqueType):
            return "UA_DATATYPEKIND_" + datatype.base_type.upper()
        if isinstance(datatype, StructType):
            for m in datatype.members:
                if m.is_optional:
                    return "UA_DATATYPEKIND_OPTSTRUCT"
            if datatype.is_union:
                return "UA_DATATYPEKIND_UNION"
            return "UA_DATATYPEKIND_STRUCTURE"
        raise RuntimeError("Unknown type")

    @staticmethod
    def get_struct_overlayable(struct):
        if not struct.pointerfree == "false":
            return "false"
        before = None
        overlayable = ""
        for m in struct.members:
            if m.is_array or not m.member_type.pointerfree:
                return "false"
            overlayable += "\n\t\t && " + m.member_type.overlayable
            if before:
                overlayable += "\n\t\t && offsetof(UA_%s, %s) == (offsetof(UA_%s, %s) + sizeof(UA_%s))" % \
                               (makeCIdentifier(struct.name), makeCIdentifier(m.name), makeCIdentifier(struct.name),
                                makeCIdentifier(before.name), makeCIdentifier(before.member_type.name))
            before = m
        return overlayable

    def get_type_overlayable(self, datatype):
        if isinstance(datatype, BuiltinType) or isinstance(datatype, OpaqueType):
            return builtin_overlayable[datatype.name] if datatype.name in builtin_overlayable else "false"
        if isinstance(datatype, EnumerationType):
            return "UA_BINARY_OVERLAYABLE_INTEGER"
        if isinstance(datatype, StructType):
            return self.get_struct_overlayable(datatype)
        raise RuntimeError("Unknown datatype")

    def print_datatype(self, datatype):
        binaryEncodingId = "0"
        if datatype.name in self.parser.typedescriptions:
            description = self.parser.typedescriptions[datatype.name]
            typeid = "{%s, %s}" % (description.namespaceid, getNodeidTypeAndId(description.nodeid))
            # xmlEncodingId = description.xmlEncodingId
            binaryEncodingId = description.binaryEncodingId
        else:
            if not self.is_internal_types:
                raise RuntimeError("NodeId for " + datatype.name + " not found in .csv file")
            else:
                typeid = "{0, UA_NODEIDTYPE_NUMERIC, {0}}"
        idName = makeCIdentifier(datatype.name)
        pointerfree = "true" if datatype.pointerfree else "false"
        return "{\n    UA_TYPENAME(\"%s\") /* .typeName */\n" % idName + \
               "    " + typeid + ", /* .typeId */\n" + \
               "    sizeof(UA_" + idName + "), /* .memSize */\n" + \
               "    " + self.get_type_index(datatype) + ", /* .typeIndex */\n" + \
               "    " + self.get_type_kind(datatype) + ", /* .typeKind */\n" + \
               "    " + pointerfree + ", /* .pointerFree */\n" + \
               "    " + self.get_type_overlayable(datatype) + ", /* .overlayable */\n" + \
               "    " + str(len(datatype.members)) + ", /* .membersSize */\n" + \
               "    " + binaryEncodingId + "LU, /* .binaryEncodingId */\n" + \
               "    %s_members" % idName + " /* .members */\n}"

    @staticmethod
    def print_members(datatype):
        idName = makeCIdentifier(datatype.name)
        if len(datatype.members) == 0:
            return "#define %s_members NULL" % (idName)
        isUnion = isinstance(datatype, StructType) and datatype.is_union
        if isUnion:
            members = "static UA_DataTypeMember %s_members[%s] = {" % (idName, len(datatype.members)-1)
        else:
            members = "static UA_DataTypeMember %s_members[%s] = {" % (idName, len(datatype.members))
        before = None
        size = len(datatype.members)
        for i, member in enumerate(datatype.members):
            if isUnion and i == 0:
                continue
            member_name = makeCIdentifier(member.name)
            member_name_capital = member_name
            if len(member_name) > 0:
                member_name_capital = member_name[0].upper() + member_name[1:]
            m = "\n{\n    UA_TYPENAME(\"%s\") /* .memberName */\n" % member_name_capital
            m += "    UA_%s_%s, /* .memberTypeIndex */\n" % (
                member.member_type.outname.upper(), makeCIdentifier(member.member_type.name.upper()))
            m += "    "
            if not before:
                m += "0,"
            elif isUnion:
                m += "sizeof(UA_UInt32),"
            else:
                if member.is_array:
                    m += "offsetof(UA_%s, %sSize)" % (idName, member_name)
                else:
                    m += "offsetof(UA_%s, %s)" % (idName, member_name)
                m += " - offsetof(UA_%s, %s)" % (idName, makeCIdentifier(before.name))
                if before.is_array or before.is_optional:
                    m += " - sizeof(void *),"
                else:
                    m += " - sizeof(UA_%s)," % makeCIdentifier(before.member_type.name)
            m += " /* .padding */\n"
            m += "    %s, /* .namespaceZero */\n" % ("true" if member.member_type.ns0 else "false")
            m += ("    true," if member.is_array else "    false,") + " /* .isArray */\n"
            m += ("    true" if member.is_optional else "    false") + " /* .isOptional */\n}"
            if i != size:
                m += ","
            members += m
            before = member
        return members + "};"

    @staticmethod
    def print_datatype_ptr(datatype):
        return "&UA_" + datatype.outname.upper() + "[UA_" + makeCIdentifier(
            datatype.outname.upper() + "_" + datatype.name.upper()) + "]"

    def print_functions(self, datatype):
        idName = makeCIdentifier(datatype.name)
        funcs = "static UA_INLINE void\nUA_%s_init(UA_%s *p) {\n    memset(p, 0, sizeof(UA_%s));\n}\n\n" % (
            idName, idName, idName)
        funcs += "static UA_INLINE UA_%s *\nUA_%s_new(void) {\n    return (UA_%s*)UA_new(%s);\n}\n\n" % (
            idName, idName, idName, CGenerator.print_datatype_ptr(datatype))
        if datatype.pointerfree == "true":
            funcs += "static UA_INLINE UA_StatusCode\nUA_%s_copy(const UA_%s *src, UA_%s *dst) {\n    *dst = *src;\n    return UA_STATUSCODE_GOOD;\n}\n\n" % (
                idName, idName, idName)
            funcs += "static UA_INLINE void\nUA_%s_deleteMembers(UA_%s *p) {\n    memset(p, 0, sizeof(UA_%s));\n}\n\n" % (
                idName, idName, idName)
            funcs += "static UA_INLINE void\nUA_%s_clear(UA_%s *p) {\n    memset(p, 0, sizeof(UA_%s));\n}\n\n" % (
                idName, idName, idName)
        else:
            for entry in whitelistFuncAttrWarnUnusedResult:
                if idName == entry:
                    funcs += "UA_INTERNAL_FUNC_ATTR_WARN_UNUSED_RESULT "
                    break

            funcs += "static UA_INLINE UA_StatusCode\nUA_%s_copy(const UA_%s *src, UA_%s *dst) {\n    return UA_copy(src, dst, %s);\n}\n\n" % (
                idName, idName, idName, self.print_datatype_ptr(datatype))
            funcs += "static UA_INLINE void\nUA_%s_deleteMembers(UA_%s *p) {\n    UA_clear(p, %s);\n}\n\n" % (
                idName, idName, self.print_datatype_ptr(datatype))
            funcs += "static UA_INLINE void\nUA_%s_clear(UA_%s *p) {\n    UA_clear(p, %s);\n}\n\n" % (
                idName, idName, self.print_datatype_ptr(datatype))
        funcs += "static UA_INLINE void\nUA_%s_delete(UA_%s *p) {\n    UA_delete(p, %s);\n}" % (
            idName, idName, self.print_datatype_ptr(datatype))
        return funcs

    def print_datatype_encoding(self, datatype):
        idName = makeCIdentifier(datatype.name)
        enc = "static UA_INLINE size_t\nUA_%s_calcSizeBinary(const UA_%s *src) {\n    return UA_calcSizeBinary(src, %s);\n}\n"
        enc += "static UA_INLINE UA_StatusCode\nUA_%s_encodeBinary(const UA_%s *src, UA_Byte **bufPos, const UA_Byte *bufEnd) {\n    return UA_encodeBinary(src, %s, bufPos, &bufEnd, NULL, NULL);\n}\n"
        enc += "static UA_INLINE UA_StatusCode\nUA_%s_decodeBinary(const UA_ByteString *src, size_t *offset, UA_%s *dst) {\n    return UA_decodeBinary(src, offset, dst, %s, NULL);\n}"
        return enc % tuple(
            list(itertools.chain(*itertools.repeat([idName, idName, self.print_datatype_ptr(datatype)], 3))))

    @staticmethod
    def print_enum_typedef(enum):
        if sys.version_info[0] < 3:
            values = enum.elements.iteritems()
        else:
            values = enum.elements.items()

        if enum.isOptionSet == True:
            return "typedef " + enum.strDataType + " " + makeCIdentifier("UA_" + enum.name) + ";\n\n" + "\n".join(
                map(lambda kv: "#define " + makeCIdentifier("UA_" + enum.name.upper() + "_" + kv[0].upper()) +
                " " + kv[1], values))
        else:
            return "typedef enum {\n    " + ",\n    ".join(
                map(lambda kv: makeCIdentifier("UA_" + enum.name.upper() + "_" + kv[0].upper()) +
                               " = " + kv[1], values)) + \
                   ",\n    __UA_{0}_FORCE32BIT = 0x7fffffff\n".format(makeCIdentifier(enum.name.upper())) + "} " + \
                   "UA_{0};\nUA_STATIC_ASSERT(sizeof(UA_{0}) == sizeof(UA_Int32), enum_must_be_32bit);".format(
                       makeCIdentifier(enum.name))

    @staticmethod
    def print_struct_typedef(struct):
        #generate enum option for union
        returnstr = ""
        if struct.is_union:
            #test = type("MyEnumOptionSet", (EnumOptionSet, object), {"foo": lambda self: "foo"})
            obj = type('MyEnumOptionSet', (object,), {'isOptionSet': False, 'elements': OrderedDict(), 'name': struct.name+"Switch"})
            obj.elements['None'] = str(0)
            count = 0
            for member in struct.members:
                if(count > 0):
                    obj.elements[member.name] = str(count)
                count += 1
            returnstr += CGenerator.print_enum_typedef(obj)
            returnstr += "\n\n"
        if len(struct.members) == 0:
            return "typedef void * UA_%s;" % makeCIdentifier(struct.name)
        returnstr += "typedef struct {\n"
        if struct.is_union:
            returnstr += "    UA_%sSwitch switchField;\n" % struct.name
            returnstr += "    union {\n"
        count = 0
        for member in struct.members:
            if member.is_array:
                returnstr += "    size_t %sSize;\n" % makeCIdentifier(member.name)
                returnstr += "    UA_%s *%s;\n" % (
                    makeCIdentifier(member.member_type.name), makeCIdentifier(member.name))
            elif struct.is_union:
                if count > 0:
                    returnstr += "        UA_%s %s;\n" % (
                    makeCIdentifier(member.member_type.name), makeCIdentifier(member.name))
            elif member.is_optional:
                returnstr += "    UA_%s *%s;\n" % (
                    makeCIdentifier(member.member_type.name), makeCIdentifier(member.name))
            else:
                returnstr += "    UA_%s %s;\n" % (
                    makeCIdentifier(member.member_type.name), makeCIdentifier(member.name))
            count += 1
        if struct.is_union:
            returnstr += "    } fields;\n"
        return returnstr + "} UA_%s;" % makeCIdentifier(struct.name)

    @staticmethod
    def print_datatype_typedef(datatype):
        if isinstance(datatype, EnumerationType):
            return CGenerator.print_enum_typedef(datatype)
        if isinstance(datatype, OpaqueType):
            return "typedef UA_" + datatype.base_type + " UA_%s;" % datatype.name
        if isinstance(datatype, StructType):
            return CGenerator.print_struct_typedef(datatype)
        raise RuntimeError("Type does not have an associated typedef")

    def write_definitions(self):
        self.fh = open(self.outfile + "_generated.h", 'w')
        self.ff = open(self.outfile + "_generated_handling.h", 'w')
        self.fe = open(self.outfile + "_generated_encoding_binary.h", 'w')
        self.fc = open(self.outfile + "_generated.c", 'w')

        self.filtered_types = self.iter_types(self.parser.types)

        self.print_header()
        self.print_handling()
        self.print_description_array()
        self.print_encoding()

        self.fh.close()
        self.ff.close()
        self.fc.close()
        self.fe.close()

    def printh(self, string):
        print(string, end='\n', file=self.fh)

    def printf(self, string):
        print(string, end='\n', file=self.ff)

    def printe(self, string):
        print(string, end='\n', file=self.fe)

    def printc(self, string):
        print(string, end='\n', file=self.fc)

    def iter_types(self, v):
        l = None
        if sys.version_info[0] < 3:
            l = list(v.itervalues())
        else:
            l = list(v.values())
        if len(self.parser.selected_types) > 0:
            l = list(filter(lambda t: t.name in self.parser.selected_types, l))
        if self.parser.no_builtin:
            l = list(filter(lambda t: not isinstance(t, BuiltinType), l))
        l = list(filter(lambda t: t.name not in self.parser.types_imported, l))
        return l

    def print_header(self):
        self.printh('''/* Generated from ''' + self.inname + ''' with script ''' + sys.argv[0] + '''
 * on host ''' + platform.uname()[1] + ''' by user ''' + getpass.getuser() + ''' at ''' + time.strftime(
            "%Y-%m-%d %I:%M:%S") + ''' */

#ifndef ''' + self.parser.outname.upper() + '''_GENERATED_H_
#define ''' + self.parser.outname.upper() + '''_GENERATED_H_

#ifdef UA_ENABLE_AMALGAMATION
#include "open62541.h"
#else
#include <open62541/types.h>
''' + ('#include <open62541/types_generated.h>\n' if self.parser.outname != "types" else '') + '''
#endif

_UA_BEGIN_DECLS

''')

        self.printh('''/**
 * Every type is assigned an index in an array containing the type descriptions.
 * These descriptions are used during type handling (copying, deletion,
 * binary encoding, ...). */''')
        self.printh("#define UA_" + self.parser.outname.upper() + "_COUNT %s" % (str(len(self.filtered_types))))

        if len(self.filtered_types) > 0:

            self.printh(
                "extern UA_EXPORT const UA_DataType UA_" + self.parser.outname.upper() + "[UA_" + self.parser.outname.upper() + "_COUNT];")

            for i, t in enumerate(self.filtered_types):
                self.printh("\n/**\n * " + t.name)
                self.printh(" * " + "^" * len(t.name))
                if t.description == "":
                    self.printh(" */")
                else:
                    self.printh(" * " + t.description + " */")
                if not isinstance(t, BuiltinType):
                    self.printh(self.print_datatype_typedef(t) + "\n")
                self.printh(
                    "#define UA_" + makeCIdentifier(self.parser.outname.upper() + "_" + t.name.upper()) + " " + str(i))

        self.printh('''

_UA_END_DECLS

#endif /* %s_GENERATED_H_ */''' % self.parser.outname.upper())

    def print_handling(self):
        self.printf('''/* Generated from ''' + self.inname + ''' with script ''' + sys.argv[0] + '''
 * on host ''' + platform.uname()[1] + ''' by user ''' + getpass.getuser() + ''' at ''' + time.strftime(
            "%Y-%m-%d %I:%M:%S") + ''' */

#ifndef ''' + self.parser.outname.upper() + '''_GENERATED_HANDLING_H_
#define ''' + self.parser.outname.upper() + '''_GENERATED_HANDLING_H_

#include "''' + self.parser.outname + '''_generated.h"

_UA_BEGIN_DECLS

#if defined(__GNUC__) && __GNUC__ >= 4 && __GNUC_MINOR__ >= 6
# pragma GCC diagnostic push
# pragma GCC diagnostic ignored "-Wmissing-field-initializers"
# pragma GCC diagnostic ignored "-Wmissing-braces"
#endif
''')

        for t in self.filtered_types:
            self.printf("\n/* " + t.name + " */")
            self.printf(self.print_functions(t))

        self.printf('''
#if defined(__GNUC__) && __GNUC__ >= 4 && __GNUC_MINOR__ >= 6
# pragma GCC diagnostic pop
#endif

_UA_END_DECLS

#endif /* %s_GENERATED_HANDLING_H_ */''' % self.parser.outname.upper())

    def print_description_array(self):
        self.printc('''/* Generated from ''' + self.inname + ''' with script ''' + sys.argv[0] + '''
 * on host ''' + platform.uname()[1] + ''' by user ''' + getpass.getuser() + ''' at ''' + time.strftime(
            "%Y-%m-%d %I:%M:%S") + ''' */

#include "''' + self.parser.outname + '''_generated.h"''')

        for t in self.filtered_types:
            self.printc("")
            self.printc("/* " + t.name + " */")
            self.printc(CGenerator.print_members(t))

        if len(self.filtered_types) > 0:
            self.printc(
                "const UA_DataType UA_%s[UA_%s_COUNT] = {" % (self.parser.outname.upper(), self.parser.outname.upper()))

            for t in self.filtered_types:
                self.printc("/* " + t.name + " */")
                self.printc(self.print_datatype(t) + ",")
            self.printc("};\n")

    def print_encoding(self):
        self.printe('''/* Generated from ''' + self.inname + ''' with script ''' + sys.argv[0] + '''
 * on host ''' + platform.uname()[1] + ''' by user ''' + getpass.getuser() + ''' at ''' + time.strftime(
            "%Y-%m-%d %I:%M:%S") + ''' */

#ifndef ''' + self.parser.outname.upper() + '''_GENERATED_ENCODING_BINARY_H_
#define ''' + self.parser.outname.upper() + '''_GENERATED_ENCODING_BINARY_H_

#ifdef UA_ENABLE_AMALGAMATION
# include "open62541.h"
#else
# include "ua_types_encoding_binary.h"
# include "''' + self.parser.outname + '''_generated.h"
#endif

''')

        for t in self.filtered_types:
            self.printe("\n/* " + t.name + " */")
            self.printe(self.print_datatype_encoding(t))

        self.printe("\n#endif /* " + self.parser.outname.upper() + "_GENERATED_ENCODING_BINARY_H_ */")
