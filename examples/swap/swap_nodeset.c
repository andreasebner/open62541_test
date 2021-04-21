/* This work is licensed under a Creative Commons CCZero 1.0 Universal License.
 * See http://creativecommons.org/publicdomain/zero/1.0/ for more information. */

#include <open62541/plugin/log_stdout.h>
#include <open62541/server.h>
#include <open62541/server_config_default.h>

/*
 * Die Dateien swap_nodeset_nodeids.h und namespace_swap_nodeset_generated.h werden durch
 * das NodesetXML aus /Informationsmodell/Output/swap_nodeset.xml sowie swap_nodeset.csv
 * generiert. Die "swap_nodeset.xml" wird durch das SIOME Tool erzeugt und sollte nicht
 * händisch angepasst werden. Die "swap_nodeset.csv" weißt den Knoten der XML NodeId's zu
 * und sorgt für die Generierung entsprechender defines mit der NodeId. Die "swap_nodeset.csv"
 * kann aktuell nicht von dem Siemens Tool erzeugt werden und muss händisch bei Änderungen
 * aktualisiert werden.
 *
 * */

#include "open62541/swap_nodeset_nodeids.h"
#include "open62541/namespace_swap_nodeset_generated.h"

#include <signal.h>
#include <stdlib.h>

UA_Boolean running = true;
UA_UInt16 nsIdx;

static void stopHandler(int sign) {
    UA_LOG_INFO(UA_Log_Stdout, UA_LOGCATEGORY_SERVER, "received ctrl-c");
    running = false;
}

/*
 * Hilfsfunktion zum "herausfinden" von dynmisch generierten NodeId's.
 */
static UA_NodeId
findSingleChildNode(UA_Server *server, UA_QualifiedName targetName,
                    UA_NodeId referenceTypeId, UA_NodeId startingNode){
    UA_NodeId resultNodeId;
    UA_RelativePathElement rpe;
    UA_RelativePathElement_init(&rpe);
    rpe.referenceTypeId = referenceTypeId;
    rpe.isInverse = false;
    rpe.includeSubtypes = false;
    rpe.targetName = targetName;
    UA_BrowsePath bp;
    UA_BrowsePath_init(&bp);
    bp.startingNode = startingNode;
    bp.relativePath.elementsSize = 1;
    bp.relativePath.elements = &rpe;
    UA_BrowsePathResult bpr =
        UA_Server_translateBrowsePathToNodeIds(server, &bp);
    if(bpr.statusCode != UA_STATUSCODE_GOOD ||
       bpr.targetsSize < 1)
        return UA_NODEID_NULL;
    if(UA_NodeId_copy(&bpr.targets[0].targetId.nodeId, &resultNodeId) != UA_STATUSCODE_GOOD){
        UA_BrowsePathResult_deleteMembers(&bpr);
        return UA_NODEID_NULL;
    }
    UA_BrowsePathResult_deleteMembers(&bpr);
    return resultNodeId;
}

/*
 * Callback zur Interaktion mit dem Zielsystem. Details in den Kommentaren der main-Funktion.
 */
static UA_StatusCode
readCurrentWorkingHours(UA_Server *server,
                        const UA_NodeId *sessionId, void *sessionContext,
                        const UA_NodeId *nodeId, void *nodeContext,
                        UA_Boolean sourceTimeStamp, const UA_NumericRange *range,
                        UA_DataValue *dataValue) {
    //collect battery state here
    UA_UInt32 workingHours = 12345;
    UA_Variant_setScalarCopy(&dataValue->value, &workingHours,
                             &UA_TYPES[UA_TYPES_UINT16]);
    dataValue->hasValue = true;
    return UA_STATUSCODE_GOOD;
}

/*
 * Callback zum Generieren eines Events. Details in den Kommentaren des "scheduleNCJob".
 */
static void resultCallack(UA_Server *server, void *data){
    UA_NodeId eventInstance;
    UA_Server_createEvent(server, UA_NODEID_NUMERIC(nsIdx, UA_SWAP_NODESETID_CNCJOBFINISHEDEVENT), &eventInstance);

    /* Der ObjektTyp wurde in SIOME modelliert und kann beliebige Informationen tragen */
    UA_DateTime eventTime = UA_DateTime_now();
    UA_Server_writeObjectProperty_scalar(server, eventInstance, UA_QUALIFIEDNAME(nsIdx, "Time"),
                                         &eventTime, &UA_TYPES[UA_TYPES_DATETIME]);
    UA_UInt16 eventSeverity = 100;
    UA_Server_writeObjectProperty_scalar(server, eventInstance, UA_QUALIFIEDNAME(0, "Severity"),
                                         &eventSeverity, &UA_TYPES[UA_TYPES_UINT16]);
    UA_String jobResult = UA_STRING("Errors:0, Finished:yes, Path-to-log:");
    UA_Server_writeObjectProperty_scalar(server, eventInstance, UA_QUALIFIEDNAME(nsIdx, "JobResult"),
                                         &jobResult, &UA_TYPES[UA_TYPES_STRING]);
    UA_Duration jobDuration = 5.0;
    UA_Server_writeObjectProperty_scalar(server, eventInstance, UA_QUALIFIEDNAME(nsIdx, "JobDuration"),
                                         &jobDuration, &UA_TYPES[UA_TYPES_DURATION]);

    /* In diesem Beispiel ist die Quelle des Events der Knoten Root/Objects/Server. OPC UA erlaubt
     * es jeden Objektknoten als Eventquelle zu deklarieren. D.h. das Event kann auch direkt von
     * der Maschinen-Instanz generiert werden.
     */
    UA_Server_triggerEvent(server, eventInstance, UA_NODEID_NUMERIC(0, UA_NS0ID_SERVER), NULL, UA_TRUE);
}

/*
 * Callback zur interaktion mit dem Zielsystem. Details in den Kommentaren der main-Funktion.
 */
static UA_StatusCode
scheduleNCJob(UA_Server *server,
                       const UA_NodeId *sessionId, void *sessionHandle,
                       const UA_NodeId *methodId, void *methodContext,
                       const UA_NodeId *objectId, void *objectContext,
                       size_t inputSize, const UA_Variant *input,
                       size_t outputSize, UA_Variant *output) {
    UA_String *nc_job_url = (UA_String*)input->data;
    /* Weitere Parameter können aus dem input parameter entnommen werden */
    //UA_UInt16 *mapping_placeholder = (UA_String*)input[1].data;
    if(nc_job_url->length > 0)
        UA_LOG_INFO(UA_Log_Stdout, UA_LOGCATEGORY_SERVER,
                    "Triggered schedule Milling with filepath %s \n", nc_job_url->data);

    /*
     * Die Methodenaufrufe im SWAP-Projekt kommen von der Ausführungsumgebung/Excecution Engine
     * und treiben den Prozess. Die Aufrufe dürfen daher nicht blockierend sein und müssen bei langen Tasks
     * vor Abschluss des eigentliches Prozesses mit einem Statuscode und ggf. weiteren Informationen
     * wie der erwarteten Ausführungszeit zurückkehren. Wenn der Task abgeschlossen wurde oder ein
     * Fehler aufgetreten ist, wird ein Event generiert, dass von der Steuerung verarbeitet werden kann.
     *
     * Exemplarisch wird nachfolgend ein Timer gestellt, der nach 5 Sekunden den resultCallback ausführt und das
     * event generiert.
     */
    UA_Server_addTimedCallback(server, resultCallack, NULL, UA_DateTime_nowMonotonic() + UA_DATETIME_SEC*5, NULL);

    UA_CncScheduleMillingResult cncScheduleMillingResult = UA_CNCSCHEDULEMILLINGRESULT_GOOD_JOB_SCHEDULED;
    UA_Variant_setScalarCopy(output, &cncScheduleMillingResult, &UA_TYPES[UA_TYPES_SWAP_NODESET_CNCSCHEDULEMILLINGRESULT]);
    UA_Duration duration = 100.0;
    UA_Variant_setScalarCopy(output+1, &duration, &UA_TYPES[UA_TYPES_DURATION]);
    return UA_STATUSCODE_GOOD;
}

int main(int argc, char** argv) {
    signal(SIGINT, stopHandler);
    signal(SIGTERM, stopHandler);

    UA_Server *server = UA_Server_new();
    UA_ServerConfig_setDefault(UA_Server_getConfig(server));

    UA_StatusCode retval;
    /* Nachfolgend wird der aus der XML generierte C-Code in den Server geladen */
    if(namespace_swap_nodeset_generated(server) != UA_STATUSCODE_GOOD) {
        UA_LOG_ERROR(UA_Log_Stdout, UA_LOGCATEGORY_SERVER, "Could not add the example nodeset. "
                                                           "Check previous output for any error.");
        retval = UA_STATUSCODE_BADUNEXPECTEDERROR;
    } else {

        /*
         * Nachfolgend wird ein neuer Namespace-Index hinzugefügt bzw. wenn der Namespace schon existiert
         * die ID zurückgegeben. Bitte im Code nicht die Namespace-ID aus der Modellierung verwenden,
         * da sich die Namespace-ID je nach Server bzw. den geladenen Informationsmodellen ändern kann.
         */
        nsIdx = UA_Server_addNamespace(server, "http://swap.fraunhofer.de");

        UA_NodeId cnc_machinetype_id = UA_NODEID_NUMERIC(nsIdx, UA_SWAP_NODESETID_CNCMASCHINETYPE);

        UA_LOG_INFO(UA_Log_Stdout, UA_LOGCATEGORY_SERVER, "Die neue Instanz hat die NodeId ns=%d;id=%d",
                    cnc_machinetype_id.namespaceIndex, cnc_machinetype_id.identifier.numeric);

        /*
         * Nachfolgend wird eine konkrete Instanz einer CNC-Maschine erzeugt. Die Kinder aus der
         * Modellierung wurden von dem SIOME Tool automatisch mit einer Richtlinie versehen, dass
         * diese beim Erzeugen des Vaterknotens ebenfalls instanziiert werden.
         */
        UA_NodeId cncMachineInstanceNodeId;
        UA_ObjectAttributes oAttr = UA_ObjectAttributes_default;
        oAttr.displayName = UA_LOCALIZEDTEXT("en-US", "CNC Machine 1");
        UA_Server_addObjectNode(server, UA_NODEID_NULL,
                                UA_NODEID_NUMERIC(0, UA_NS0ID_OBJECTSFOLDER),
                                UA_NODEID_NUMERIC(0, UA_NS0ID_ORGANIZES),
                                UA_QUALIFIEDNAME(nsIdx, "CNC Machine 1"),
                                cnc_machinetype_id,
                                oAttr, NULL, &cncMachineInstanceNodeId);
        /*
         * Aktuell wurde noch keine Instanz der Jobverwaltung erzeugt. Der Typ ist auf dem
         * Server unterhalb Root/Types/ObjectTypes/BaseObjectType/CNCJobManagement zu finden.
         * Task -> Instanzen gemäß gewünschter Umgebung anlegen.
         */

        /*
         * Die Instanz der CNC Maschine wurde erzeugt und ist unterhalb Root/Objects zu finden.
         * Bisher wurden die Felder und Methoden nicht mit Inhalten bzw. Funktionalität versehen.
         * Für jeden Kindknoten wurde eine NodeId generiert, die für weitere Schritte herausgesucht
         * werden muss.
         */
        UA_NodeId serialNumberNodeId = UA_NODEID_NULL;
        serialNumberNodeId = findSingleChildNode(server, UA_QUALIFIEDNAME(nsIdx, "MachineParameters"),
                                                 UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT), cncMachineInstanceNodeId);
        serialNumberNodeId = findSingleChildNode(server, UA_QUALIFIEDNAME(nsIdx, "SerialNumber"),
                                                 UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT), serialNumberNodeId);

        /*
         * Der Wert von Variablenknoten kann direkt geschrieben werden.
         * Für statische Werte, kann z.B. eine Datei ausgelesen werden und diese Werte initial
         * geschrieben werden.
         */
        UA_String serialNumber = UA_STRING("0X-123-AA");
        UA_Variant value;
        UA_Variant_init(&value);
        UA_Variant_setScalar(&value, &serialNumber, &UA_TYPES[UA_TYPES_STRING]);
        UA_Server_writeValue(server, serialNumberNodeId, value);

        /*
         * Statt dem direkten schreiben des Wertes in das Informationsmodell, kann dem System
         * auch ein Callback hinterlegt werden, der bei einer Anfrage den Wert "beschafft".
         * Dieser Mechanismus eignet sich insbesondere für dynamische Inhalte.
         * Details finden sich hier: https://open62541.org/doc/current/tutorial_server_datasource.html
         */
        UA_NodeId workingHoursId = UA_NODEID_NULL;
        workingHoursId = findSingleChildNode(server, UA_QUALIFIEDNAME(nsIdx, "MachineParameters"), UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT), cncMachineInstanceNodeId);
        workingHoursId = findSingleChildNode(server, UA_QUALIFIEDNAME(nsIdx, "SerialNumber"), UA_NODEID_NUMERIC(0, UA_NS0ID_HASCOMPONENT), workingHoursId);

        UA_DataSource workingHourDataSource;
        workingHourDataSource.read = readCurrentWorkingHours;
        workingHourDataSource.write = NULL;
        UA_Server_setVariableNode_dataSource(server, workingHoursId, workingHourDataSource);

        /*
         * Neben den Variablen müssen noch die Methoden des Informationsmodell mit Logik versehen werden.
         * Die Verknüfung erfolgt auf Basis des Typen, d.h. die Methoden von Instanzen verweisen auf die
         * Methoden des Typs. Details: https://open62541.org/doc/current/tutorial_server_method.html
         */
        UA_Server_setMethodNode_callback(server, UA_NODEID_NUMERIC(nsIdx, UA_SWAP_NODESETID_SCHEDULEMILLING), scheduleNCJob);


        retval = UA_Server_run(server, &running);
    }

    UA_Server_delete(server);
    return retval == UA_STATUSCODE_GOOD ? EXIT_SUCCESS : EXIT_FAILURE;
}

