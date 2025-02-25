import logging

from Microsoft.AnalysisServices.Tabular import (
    Server,
    RefreshType,
    ColumnType,
    Table,
    DataColumn,
    Partition,
    MPartitionSource,
    Measure,
)
from Microsoft.AnalysisServices.AdomdClient import AdomdCommand, AdomdConnection
from Microsoft.AnalysisServices import UpdateOptions
from typing import Any, Dict, List, Union
from collections import namedtuple
import pandas as pd
import os
import subprocess
import atexit
from logic_utils import (
    pd_dataframe_to_m_expression,
    pandas_datatype_to_tabular_datatype,
    ticks_to_datetime,
    remove_suffix,
)
from query import Connection
from table import PyTable, PyTables
from tabular_tracing import Refresh_Trace

logger = logging.getLogger("PyTabular")


class Tabular:
    """Tabular Class to perform operations: [Microsoft.AnalysisServices.Tabular](https://docs.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular?view=analysisservices-dotnet). You can use this class as your main way to interact with your model.

    Args:
            CONNECTION_STR (str): Valid [Connection String](https://docs.microsoft.com/en-us/analysis-services/instances/connection-string-properties-analysis-services?view=asallproducts-allversions) for connecting to a Tabular Model.
    Attributes:
            Server (Server): See [Server MS Docs](https://docs.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.server?view=analysisservices-dotnet).
            Catalog (str): Name of Database. See [Catalog MS Docs](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.connectioninfo.catalog?view=analysisservices-dotnet#microsoft-analysisservices-connectioninfo-catalog).
            Model (Model): See [Model MS Docs](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.model?view=analysisservices-dotnet).
            AdomdConnection (AdomdConnection): For querying. See [AdomdConnection MS Docs](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.adomdclient.adomdconnection?view=analysisservices-dotnet). Connection made from parts of the originally provided connection string.
            Tables (PyTables[PyTable]): Wrappers for [Table MS Docs](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.table?view=analysisservices-dotnet). So you have the full capabilities of what the MS Docs offer and a few others. Like `Tabular().Tables['Table Name'].Row_Count()`. Or you can find a table via `Tabular().Tables[0]` or `Tabular().Tables['Table Name']`
            Columns (List[Column]): Easy access list of columns from model. See [Column MS Docs](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.column?view=analysisservices-dotnet).
            Partitions (List[Partition]): Easy access list of partitions from model. See [Partition MS Docs](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.partition?view=analysisservices-dotnet).
            Measures (List[Measure]): Easy access list of measures from model. See [Measure MS Docs](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.table.measures?view=analysisservices-dotnet#microsoft-analysisservices-tabular-table-measures).
    """

    def __init__(self, CONNECTION_STR: str):
        logger.debug("Initializing Tabular Class")
        self.Server = Server()
        self.Server.Connect(CONNECTION_STR)
        logger.info(f"Connected to Server - {self.Server.Name}")
        self.Catalog = self.Server.ConnectionInfo.Catalog
        logger.debug(f"Received Catalog - {self.Catalog}")
        try:
            self.Database = [
                database
                for database in self.Server.Databases.GetEnumerator()
                if database.Name == self.Catalog
            ][0]
        except Exception:
            err_msg = f"Unable to find Database... {self.Catalog}"
            logger.error(err_msg)
            raise Exception(err_msg)
        logger.info(f"Connected to Database - {self.Database.Name}")
        self.CompatibilityLevel: int = self.Database.CompatibilityLevel
        self.CompatibilityMode: int = self.Database.CompatibilityMode.value__
        self.Model = self.Database.Model
        logger.info(f"Connected to Model - {self.Model.Name}")
        self.Adomd = Connection(self.Server)
        self.Reload_Model_Info()
        logger.debug("Class Initialization Completed")
        logger.debug("Registering Disconnect on Termination...")
        atexit.register(self.Disconnect)

        pass

    def __repr__(self) -> str:
        return f"{self.Server.Name}::{self.Database.Name}::{self.Model.Name}\nEstimated Size::{self.Database.EstimatedSize}\nTables::{len(self.Tables)}\nColumns::{len(self.Columns)}\nPartitions::{len(self.Partitions)}\nMeasures::{len(self.Measures)}"

    def Reload_Model_Info(self) -> bool:
        """Runs on __init__ iterates through details, can be called after any model changes. Called in SaveChanges()

        Returns:
                bool: True if successful
        """
        self.Tables = PyTables(
            [PyTable(table, self) for table in self.Model.Tables.GetEnumerator()]
        )
        self.Measures = [
            measure
            for table in self.Tables
            for measure in table.Measures.GetEnumerator()
        ]
        self.Database.Refresh()
        return True

    def Is_Process(self) -> bool:
        """Run method to check if Processing is occurring. Will query DMV $SYSTEM.DISCOVER_JOBS to see if any processing is happening.

        Returns:
                bool: True if DMV shows Process, False if not.
        """
        _jobs_df = self.Query("select * from $SYSTEM.DISCOVER_JOBS")
        return (
            True
            if len(_jobs_df[_jobs_df["JOB_DESCRIPTION"] == "Process"]) > 0
            else False
        )

    def Disconnect(self) -> bool:
        """Disconnects from Model

        Returns:
                bool: True if successful
        """
        logger.debug(f"Disconnecting from - {self.Server.Name}")
        return self.Server.Disconnect()

    def Refresh(
        self,
        Object: Union[str, Table, Partition, Dict[str, Any]],
        RefreshType: RefreshType = RefreshType.Full,
        Tracing=False,
    ) -> None:
        """Refreshes table(s) and partition(s).

        Args:
                Object (Union[ str, Table, Partition, Dict[str, Any], Iterable[str, Table, Partition, Dict[str, Any]] ]): Designed to handle a few different ways of selecting a refresh.
                str == 'Table_Name'
                Table == Table Object
                Partition == Partition Object
                Dict[str, Any] == A way to specify a partition of group of partitions. For ex. {'Table_Name':'Partition1'} or {'Table_Name':['Partition1','Partition2']}. NOTE you can also change out the strings for partition or tables objects.
                RefreshType (RefreshType, optional): See [RefreshType](https://docs.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.refreshtype?view=analysisservices-dotnet). Defaults to RefreshType.Full.
                Tracing (bool, optional): Currently just some basic tracing to track refreshes. Defaults to False.

        Raises:
                Exception: Raises exception if unable to find table or partition via string.


        Returns:
                WIP: WIP
        """
        logger.debug("Beginning RequestRefresh cadence...")

        def _Refresh_Report(Property_Changes) -> pd.DataFrame:
            logger.debug("Running Refresh Report...")
            refresh_data = []
            for property_change in Property_Changes:
                if (
                    isinstance(property_change.Object, Partition)
                    and property_change.Property_Name == "RefreshedTime"
                ):
                    table, partition, refreshed_time = (
                        property_change.Object.Table.Name,
                        property_change.Object.Name,
                        ticks_to_datetime(property_change.New_Value.Ticks),
                    )
                    logger.info(
                        f'{table} - {partition} Refreshed! - {refreshed_time.strftime("%m/%d/%Y, %H:%M:%S")}'
                    )
                    refresh_data += [[table, partition, refreshed_time]]
            return pd.DataFrame(
                refresh_data, columns=["Table", "Partition", "Refreshed Time"]
            )

        def refresh_table(table: Table) -> None:
            logging.info(f"Requesting refresh for {table.Name}")
            table.RequestRefresh(RefreshType)

        def refresh_partition(partition: Partition) -> None:
            logging.info(
                f"Requesting refresh for {partition.Table.Name}|{partition.Name}"
            )
            partition.RequestRefresh(RefreshType)

        def refresh_dict(partition_dict: Dict) -> None:
            for table in partition_dict.keys():
                table_object = find_table(table) if isinstance(table, str) else table

                def handle_partitions(object):
                    if isinstance(object, str):
                        refresh_partition(find_partition(table_object, object))
                    elif isinstance(object, Partition):
                        refresh_partition(object)
                    else:
                        [handle_partitions(obj) for obj in object]

                handle_partitions(partition_dict[table])

        def find_table(table_str: str) -> Table:
            result = self.Model.Tables.Find(table_str)
            if result is None:
                raise Exception(f"Unable to find table! from {table_str}")
            logging.debug(f"Found table {result.Name}")
            return result

        def find_partition(table: Table, partition_str: str) -> Partition:
            result = table.Partitions.Find(partition_str)
            if result is None:
                raise Exception(
                    f"Unable to find partition! {table.Name}|{partition_str}"
                )
            logging.debug(f"Found partition {result.Table.Name}|{result.Name}")
            return result

        def refresh(Object):
            if isinstance(Object, str):
                refresh_table(find_table(Object))
            elif isinstance(Object, Dict):
                refresh_dict(Object)
            elif isinstance(Object, Table):
                refresh_table(Object)
            elif isinstance(Object, Partition):
                refresh_partition(Object)
            else:
                [refresh(object) for object in Object]

        refresh(Object)
        if Tracing:
            rt = Refresh_Trace(self)
            rt.Start()

        m = self.SaveChanges()

        if Tracing:
            rt.Stop()
            rt.Drop()

        return _Refresh_Report(m.Property_Changes)

    def Update(self, UpdateOptions: UpdateOptions = UpdateOptions.ExpandFull) -> None:
        """[Update Model](https://docs.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.majorobject.update?view=analysisservices-dotnet#microsoft-analysisservices-majorobject-update(microsoft-analysisservices-updateoptions))

        Args:
                UpdateOptions (UpdateOptions, optional): See above MS Doc link. Defaults to UpdateOptions.ExpandFull.

        Returns:
                None: Placeholder to eventually change.
        """
        logger.debug("Running Update Request")
        return self.Database.Update(UpdateOptions)

    def SaveChanges(self) -> bool:
        def property_changes(Property_Changes):
            Property_Change = namedtuple(
                "Property_Change",
                "New_Value Object Original_Value Property_Name Property_Type",
            )
            return [
                Property_Change(
                    change.NewValue,
                    change.Object,
                    change.OriginalValue,
                    change.PropertyName,
                    change.PropertyType,
                )
                for change in Property_Changes.GetEnumerator()
            ]

        logger.info("Executing SaveChanges()...")
        Model_Save_Results = self.Model.SaveChanges()
        if isinstance(Model_Save_Results.Impact, type(None)):
            logger.warning(f"No changes detected on save for {self.Server.Name}")
            return None
        else:
            Property_Changes = Model_Save_Results.Impact.PropertyChanges
            Added_Objects = Model_Save_Results.Impact.AddedObjects
            Added_Subtree_Roots = Model_Save_Results.Impact.AddedSubtreeRoots
            Removed_Objects = Model_Save_Results.Impact.RemovedObjects
            Removed_Subtree_Roots = Model_Save_Results.Impact.RemovedSubtreeRoots
            Xmla_Results = Model_Save_Results.XmlaResults
            Changes = namedtuple(
                "Changes",
                "Property_Changes Added_Objects Added_Subtree_Roots Removed_Objects Removed_Subtree_Roots Xmla_Results",
            )
            [
                property_changes(Property_Changes),
                Added_Objects,
                Added_Subtree_Roots,
                Removed_Objects,
                Removed_Subtree_Roots,
                Xmla_Results,
            ]
            self.Reload_Model_Info()
            return Changes(
                property_changes(Property_Changes),
                Added_Objects,
                Added_Subtree_Roots,
                Removed_Objects,
                Removed_Subtree_Roots,
                Xmla_Results,
            )

    def Backup_Table(self, table_str: str) -> bool:
        """USE WITH CAUTION, EXPERIMENTAL. Backs up table in memory, brings with it measures, columns, hierarchies, relationships, roles, etc.
        It will add suffix '_backup' to all objects.
        Refresh is performed from source during backup.

        Args:
                table_str (str, optional): Name of Table.

        Returns:
                bool: Returns True if Successful, else will return error.
        """
        logger.info("Backup Beginning...")
        logger.debug(f"Cloning {table_str}")
        table = self.Model.Tables.Find(table_str).Clone()
        logger.info("Beginning Renames")

        def rename(items):
            for item in items:
                item.RequestRename(f"{item.Name}_backup")
                logger.debug(f"Renamed - {item.Name}")

        logger.info("Renaming Columns")
        rename(table.Columns.GetEnumerator())
        logger.info("Renaming Partitions")
        rename(table.Partitions.GetEnumerator())
        logger.info("Renaming Measures")
        rename(table.Measures.GetEnumerator())
        logger.info("Renaming Hierarchies")
        rename(table.Hierarchies.GetEnumerator())
        logger.info("Renaming Table")
        table.RequestRename(f"{table.Name}_backup")
        logger.info("Adding Table to Model as backup")
        self.Model.Tables.Add(table)
        logger.info("Finding Necessary Relationships... Cloning...")
        relationships = [
            relationship.Clone()
            for relationship in self.Model.Relationships.GetEnumerator()
            if relationship.ToTable.Name == remove_suffix(table.Name, "_backup")
            or relationship.FromTable.Name == remove_suffix(table.Name, "_backup")
        ]
        logger.info("Renaming Relationships")
        rename(relationships)
        logger.info("Switching Relationships to Clone Table & Column")
        for relationship in relationships:
            logger.debug(f"Renaming - {relationship.Name}")
            if relationship.ToTable.Name == remove_suffix(table.Name, "_backup"):
                relationship.set_ToColumn(
                    table.Columns.Find(f"{relationship.ToColumn.Name}_backup")
                )
            elif relationship.FromTable.Name == remove_suffix(table.Name, "_backup"):
                relationship.set_FromColumn(
                    table.Columns.Find(f"{relationship.FromColumn.Name}_backup")
                )
            logger.debug(f"Adding {relationship.Name} to {self.Model.Name}")
            self.Model.Relationships.Add(relationship)

        def clone_role_permissions():
            logger.info("Beginning to handle roles and permissions for table...")
            logger.debug("Finding Roles...")
            roles = [
                role
                for role in self.Model.Roles.GetEnumerator()
                for tablepermission in role.TablePermissions.GetEnumerator()
                if tablepermission.Name == table_str
            ]
            for role in roles:
                logger.debug(f"Role {role.Name} matched, looking into it...")
                logger.debug("Searching for table specific permissions")
                tablepermissions = [
                    table.Clone()
                    for table in role.TablePermissions.GetEnumerator()
                    if table.Name == table_str
                ]
                for tablepermission in tablepermissions:
                    logger.debug(
                        f"{tablepermission.Name} found... switching table to clone"
                    )
                    tablepermission.set_Table(table)
                    for column in tablepermission.ColumnPermissions.GetEnumerator():
                        logger.debug(
                            f"Column - {column.Name} copying permissions to clone..."
                        )
                        column.set_Column(
                            self.Model.Tables.Find(table.Name).Columns.Find(
                                f"{column.Name}_backup"
                            )
                        )
                    logger.debug(f"Adding {tablepermission.Name} to {role.Name}")
                    role.TablePermissions.Add(tablepermission)
            return True

        clone_role_permissions()
        logger.info(f"Refreshing Clone... {table.Name}")
        self.Refresh([table])
        logger.info(f"Updating Model {self.Model.Name}")
        self.SaveChanges()
        return True

    def Revert_Table(self, table_str: str) -> bool:
        """USE WITH CAUTION, EXPERIMENTAL. This is used in conjunction with Backup_Table().
        It will take the 'TableName_backup' and replace with the original.
        Example scenario ->
        1. model.Backup_Table('TableName')
        2. perform any proposed changes in original 'TableName'
        3. validate changes in 'TableName'
        4. if unsuccessful run model.Revert_Table('TableName')

        Args:
                table_str (str): Name of table.

        Returns:
                bool: Returns True if Successful, else will return error.
        """
        logger.info(f"Beginning Revert for {table_str}")
        logger.debug(f"Finding original {table_str}")
        main = self.Model.Tables.Find(table_str)
        logger.debug(f"Finding backup {table_str}")
        backup = self.Model.Tables.Find(f"{table_str}_backup")
        logger.debug("Finding original relationships")
        main_relationships = [
            relationship
            for relationship in self.Model.Relationships.GetEnumerator()
            if relationship.ToTable.Name == main.Name
            or relationship.FromTable.Name == main.Name
        ]
        logger.debug("Finding backup relationships")
        backup_relationships = [
            relationship
            for relationship in self.Model.Relationships.GetEnumerator()
            if relationship.ToTable.Name == backup.Name
            or relationship.FromTable.Name == backup.Name
        ]

        def remove_role_permissions():
            logger.debug(
                f"Finding table and column permission in roles to remove from {table_str}"
            )
            roles = [
                role
                for role in self.Model.Roles.GetEnumerator()
                for tablepermission in role.TablePermissions.GetEnumerator()
                if tablepermission.Name == table_str
            ]
            for role in roles:
                logger.debug(f"Role {role.Name} Found")
                tablepermissions = [
                    table
                    for table in role.TablePermissions.GetEnumerator()
                    if table.Name == table_str
                ]
                for tablepermission in tablepermissions:
                    logger.debug(f"Removing {tablepermission.Name} from {role.Name}")
                    role.TablePermissions.Remove(tablepermission)

        for relationship in main_relationships:
            logger.debug("Cleaning relationships...")
            if relationship.ToTable.Name == main.Name:
                logger.debug(f"Removing {relationship.Name}")
                self.Model.Relationships.Remove(relationship)
            elif relationship.FromTable.Name == main.Name:
                logger.debug(f"Removing {relationship.Name}")
                self.Model.Relationships.Remove(relationship)
        logger.debug(f"Removing Original Table {main.Name}")
        self.Model.Tables.Remove(main)
        remove_role_permissions()

        def dename(items):
            for item in items:
                logger.debug(f"Removing Suffix for {item.Name}")
                item.RequestRename(remove_suffix(item.Name, "_backup"))
                logger.debug(f"Saving Changes... for {item.Name}")
                self.Model.SaveChanges()

        logger.info("Name changes for Columns...")
        dename(
            [
                column
                for column in backup.Columns.GetEnumerator()
                if column.Type != ColumnType.RowNumber
            ]
        )
        logger.info("Name changes for Partitions...")
        dename(backup.Partitions.GetEnumerator())
        logger.info("Name changes for Measures...")
        dename(backup.Measures.GetEnumerator())
        logger.info("Name changes for Hierarchies...")
        dename(backup.Hierarchies.GetEnumerator())
        logger.info("Name changes for Relationships...")
        dename(backup_relationships)
        logger.info("Name changes for Backup Table...")
        backup.RequestRename(remove_suffix(backup.Name, "_backup"))
        self.SaveChanges()
        return True

    def Query(self, Query_Str: str) -> Union[pd.DataFrame, str, int]:
        """Executes Query on Model and Returns Results in Pandas DataFrame

        Args:
                Query_Str (str): Dax Query. Note, needs full syntax (ex: EVALUATE). See (DAX Queries)[https://docs.microsoft.com/en-us/dax/dax-queries].
                Will check if query string is a file. If it is, then it will perform a query on whatever is read from the file.
                It is also possible to query DMV. For example. Query("select * from $SYSTEM.DISCOVER_TRACE_EVENT_CATEGORIES"). See (DMVs)[https://docs.microsoft.com/en-us/analysis-services/instances/use-dynamic-management-views-dmvs-to-monitor-analysis-services?view=asallproducts-allversions]

        Returns:
                pd.DataFrame: Returns dataframe with results
        """
        return self.Adomd.Query(Query_Str)

    def Query_Every_Column(
        self, query_function: str = "COUNTROWS(VALUES(_))"
    ) -> pd.DataFrame:
        """This will dynamically create a query to pull all columns from the model and run the query function. It will replace the _ with the column to run.

        Args:
                query_function (str, optional): Dax query is dynamically building a query with the UNION & ROW DAX Functions.

        Returns:
                pd.DataFrame: Returns dataframe with results.
        """
        logger.info("Beginning execution of querying every column...")
        logger.debug(f"Function to be run: {query_function}")
        logger.debug("Dynamically creating DAX query...")
        query_str = "EVALUATE UNION(\n"
        columns = [column for table in self.Tables for column in table.Columns]
        for column in columns:
            if column.Type != ColumnType.RowNumber:
                table_name = column.Table.get_Name()
                column_name = column.get_Name()
                dax_identifier = f"'{table_name}'[{column_name}]"
                query_str += f"ROW(\"Table\",\"{table_name}\",\"Column\",\"{column_name}\",\"{query_function}\",{query_function.replace('_',dax_identifier)}),\n"
        query_str = f"{query_str[:-2]})"
        return self.Query(query_str)

    def Query_Every_Table(self, query_function: str = "COUNTROWS(_)") -> pd.DataFrame:
        """This will dynamically create a query to pull all tables from the model and run the query function.
        It will replace the _ with the table to run.

        Args:
                query_function (str, optional): Dax query is dynamically building a query with the UNION & ROW DAX Functions. Defaults to 'COUNTROWS(_)'.

        Returns:
                pd.DataFrame: Returns dataframe with results
        """
        logger.info("Beginning execution of querying every table...")
        logger.debug(f"Function to be run: {query_function}")
        logger.debug("Dynamically creating DAX query...")
        query_str = "EVALUATE UNION(\n"
        for table in self.Tables:
            table_name = table.get_Name()
            dax_table_identifier = f"'{table_name}'"
            query_str += f"ROW(\"Table\",\"{table_name}\",\"{query_function}\",{query_function.replace('_',dax_table_identifier)}),\n"
        query_str = f"{query_str[:-2]})"
        return self.Query(query_str)

    def Analyze_BPA(
        self, Tabular_Editor_Exe: str, Best_Practice_Analyzer: str
    ) -> List[str]:
        """Takes your Tabular Model and performs TE2s BPA. Runs through Command line.
        [Tabular Editor BPA](https://docs.tabulareditor.com/te2/Best-Practice-Analyzer.html)
        [Tabular Editor Command Line Options](https://docs.tabulareditor.com/te2/Command-line-Options.html)

        Args:
                Tabular_Editor_Exe (str): TE2 Exe File path. Feel free to use class TE2().EXE_Path or provide your own.
                Best_Practice_Analyzer (str): BPA json file path. Feel free to use class BPA().Location or provide your own. Defualts to 	https://raw.githubusercontent.com/microsoft/Analysis-Services/master/BestPracticeRules/BPARules.json

        Returns:
                List[str]: Assuming no failure, will return list of BPA violations. Else will return error from command line.
        """
        logger.debug("Beginning request to talk with TE2 & Find BPA...")
        cmd = f'{Tabular_Editor_Exe} "Provider=MSOLAP;{self.Adomd.ConnectionString}" {self.Database.Name} -B "{os.getcwd()}\\Model.bim" -A {Best_Practice_Analyzer} -V/?'
        logger.debug("Command Generated")
        logger.debug("Submitting Command...")
        sp = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        raw_output, error = sp.communicate()
        if len(error) > 0:
            return error
        else:
            return [
                output for output in raw_output.split("\n") if "violates rule" in output
            ]

    def Create_Table(self, df: pd.DataFrame, table_name: str) -> bool:
        """Creates tables from pd.DataFrame as an M-Partition.
        So will convert the dataframe to M-Partition logic via the M query table constructor.
        Runs refresh and will update model.

        Args:
                df (pd.DataFrame): DataFrame to add to model
                table_name (str): _description_

        Returns:
                bool: True if successful
        """
        logger.debug(f"Beginning to create table for {table_name}...")
        new_table = Table()
        new_table.RequestRename(table_name)
        logger.debug("Sorting through columns...")
        df_column_names = df.columns
        dtype_conversion = pandas_datatype_to_tabular_datatype(df)
        for df_column_name in df_column_names:
            logger.debug(f"Adding {df_column_name} to Table...")
            column = DataColumn()
            column.RequestRename(df_column_name)
            column.set_SourceColumn(df_column_name)
            column.set_DataType(dtype_conversion[df_column_name])
            new_table.Columns.Add(column)
        logger.debug("Expression String Created...")
        logger.debug("Creating MPartition...")
        partition = Partition()
        partition.set_Source(MPartitionSource())
        logger.debug("Setting MPartition Expression...")
        partition.Source.set_Expression(pd_dataframe_to_m_expression(df))
        logger.debug(
            f"Adding partition: {partition.Name} to {self.Server.Name}::{self.Database.Name}::{self.Model.Name}"
        )
        new_table.Partitions.Add(partition)
        logger.debug(
            f"Adding table: {new_table.Name} to {self.Server.Name}::{self.Database.Name}::{self.Model.Name}"
        )
        self.Model.Tables.Add(new_table)
        self.Refresh([new_table])
        self.SaveChanges()
        return True
