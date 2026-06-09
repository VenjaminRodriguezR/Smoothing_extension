import logging
import time
import os
import vtk
import re 
import qt
import ctk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper

from slicer import vtkMRMLScalarVolumeNode, vtkMRMLSegmentationNode
from typing import Annotated
from slicer.parameterNodeWrapper import parameterNodeWrapper, Choice, WithinRange
#
# Smoothing
#


class Smoothing(ScriptedLoadableModule):
    """GUI-based segmentation smoothing module for 3D Slicer."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)

        self.parent.title = _("Smoothing")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Segmentation")]
        self.parent.dependencies = ["Segmentations", "SegmentEditor"]
        self.parent.contributors = ["Ben"]

        self.parent.helpText = _("""
Smoothing provides GUI-based postprocessing tools for smoothing 3D Slicer segmentations.
This version allows the user to select one or more smoothing methods from the GUI.
Each selected method is applied independently to a copy of the original segmentation.""")

        self.parent.acknowledgementText = _("""
This module was developed as a 3D Slicer scripted extension for segmentation postprocessing.
""")


#
# SmoothingParameterNode
#

@parameterNodeWrapper
class SmoothingParameterNode:
    """Parameters for GUI-based segmentation smoothing."""

    inputSegmentation: vtkMRMLSegmentationNode
    referenceVolume: vtkMRMLScalarVolumeNode
    outputSegmentation: vtkMRMLSegmentationNode

    applyScope: Annotated[
        str,
        Choice(["VISIBLE_SEGMENTS", "ALL_SEGMENTS"])
    ] = "VISIBLE_SEGMENTS"

    medianEnabled: bool = False
    openingEnabled: bool = False
    closingEnabled: bool = False
    gaussianEnabled: bool = False
    jointTaubinEnabled: bool = True

    medianKernelSizeMm: Annotated[float, WithinRange(0.1, 20.0)] = 3.0
    openingKernelSizeMm: Annotated[float, WithinRange(0.1, 20.0)] = 3.0
    closingKernelSizeMm: Annotated[float, WithinRange(0.1, 20.0)] = 3.0
    gaussianStandardDeviationMm: Annotated[float, WithinRange(0.1, 10.0)] = 1.0
    jointTaubinSmoothingFactor: Annotated[float, WithinRange(0.01, 1.0)] = 0.5

    overwriteInput: bool = False
#
# SmoothingWidget
#


class SmoothingWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Module GUI."""

    def __init__(self, parent=None) -> None:
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)

        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

    def setup(self) -> None:
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/Smoothing.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = SmoothingLogic()

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        self.setupBatchGui()
        self.setupProgressGui()
        self.setupGuiDefaults()
        self.setupConnections()

        self.initializeParameterNode()
    
    def setupProgressGui(self) -> None:
        """
        Add progress bars for normal smoothing and batch processing.
        """

        self.progressCollapsibleButton = ctk.ctkCollapsibleButton()
        self.progressCollapsibleButton.text = "Progress"
        self.progressCollapsibleButton.collapsed = False

        # Put progress near the bottom, before the final spacer if possible.
        self.layout.insertWidget(self.layout.count() - 1, self.progressCollapsibleButton)

        progressLayout = qt.QFormLayout(self.progressCollapsibleButton)

        self.batchProgressBar = qt.QProgressBar()
        self.batchProgressBar.minimum = 0
        self.batchProgressBar.maximum = 100
        self.batchProgressBar.value = 0
        self.batchProgressBar.textVisible = True
        progressLayout.addRow("Batch:", self.batchProgressBar)
        
    def setupBatchGui(self) -> None:
        """
        Add batch-processing controls programmatically.

        This avoids having to manually edit Smoothing.ui for now.
        Batch mode processes a folder containing paired files such as:

            Volume_001.nrrd
            Segmentation_001.seg.nrrd

        and writes one output segmentation per selected smoothing method.
        """

        self.batchCollapsibleButton = ctk.ctkCollapsibleButton()
        self.batchCollapsibleButton.text = "Batch processing"
        self.batchCollapsibleButton.collapsed = True
        self.layout.addWidget(self.batchCollapsibleButton)

        batchLayout = qt.QFormLayout(self.batchCollapsibleButton)

        self.batchModeCheckBox = qt.QCheckBox()
        self.batchModeCheckBox.text = "Enable batch processing from folder"
        self.batchModeCheckBox.checked = False
        batchLayout.addRow(self.batchModeCheckBox)

        self.batchInputFolderLineEdit = qt.QLineEdit()
        self.batchInputFolderLineEdit.placeholderText = "Folder containing Volume_XXX and Segmentation_XXX files"

        self.batchInputFolderButton = qt.QPushButton("Browse...")
        inputFolderLayout = qt.QHBoxLayout()
        inputFolderLayout.addWidget(self.batchInputFolderLineEdit)
        inputFolderLayout.addWidget(self.batchInputFolderButton)
        batchLayout.addRow("Input folder:", inputFolderLayout)

        self.batchOutputFolderLineEdit = qt.QLineEdit()
        self.batchOutputFolderLineEdit.placeholderText = "Folder where smoothed segmentations will be saved"

        self.batchOutputFolderButton = qt.QPushButton("Browse...")
        outputFolderLayout = qt.QHBoxLayout()
        outputFolderLayout.addWidget(self.batchOutputFolderLineEdit)
        outputFolderLayout.addWidget(self.batchOutputFolderButton)
        batchLayout.addRow("Output folder:", outputFolderLayout)

        self.batchRecursiveCheckBox = qt.QCheckBox()
        self.batchRecursiveCheckBox.text = "Search recursively"
        self.batchRecursiveCheckBox.checked = True
        batchLayout.addRow(self.batchRecursiveCheckBox)

        self.batchKeepLoadedNodesCheckBox = qt.QCheckBox()
        self.batchKeepLoadedNodesCheckBox.text = "Keep loaded batch nodes in scene"
        self.batchKeepLoadedNodesCheckBox.checked = False
        batchLayout.addRow(self.batchKeepLoadedNodesCheckBox)

        self.batchModeCheckBox.connect("toggled(bool)", self.onBatchModeChanged)
        self.batchInputFolderButton.connect("clicked(bool)", self.onBrowseBatchInputFolder)
        self.batchOutputFolderButton.connect("clicked(bool)", self.onBrowseBatchOutputFolder)
        self.batchInputFolderLineEdit.connect("textChanged(QString)", self._checkCanApply)
        self.batchOutputFolderLineEdit.connect("textChanged(QString)", self._checkCanApply)
        self.batchRecursiveCheckBox.connect("toggled(bool)", self._checkCanApply)

    def resetProgressBars(self) -> None:
        """Reset progress bars."""

        if hasattr(self, "batchProgressBar"):
            self.batchProgressBar.setValue(0)
            self.batchProgressBar.repaint()

        slicer.app.processEvents()


    def updateBatchProgress(self, value, text=None) -> None:
        """Update batch progress bar."""

        value = max(0, min(100, int(value)))

        if hasattr(self, "batchProgressBar"):
            self.batchProgressBar.setValue(value)
            self.batchProgressBar.repaint()

        if text and hasattr(self.ui, "statusLabel"):
            self.ui.statusLabel.text = text
            self.ui.statusLabel.repaint()

        slicer.util.showStatusMessage(text or f"Batch progress: {value}%")
        slicer.app.processEvents()
        
    def onBatchModeChanged(self, checked=False) -> None:
        """
        Update GUI state when batch mode is enabled or disabled.

        In batch mode, the input/output MRML selectors are not required.
        The selected folder is used instead.
        """

        batchMode = self.batchModeCheckBox.checked

        self.ui.inputSegmentationSelector.enabled = not batchMode
        self.ui.referenceVolumeSelector.enabled = not batchMode
        self.ui.outputSegmentationSelector.enabled = not batchMode
        self.ui.overwriteInputCheckBox.enabled = not batchMode

        if batchMode:
            self.ui.overwriteInputCheckBox.checked = False

        self.updateOutputVisibility()
        self._checkCanApply()


    def onBrowseBatchInputFolder(self, checked=False) -> None:
        folderPath = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(),
            "Select batch input folder",
            self.batchInputFolderLineEdit.text,
        )

        if folderPath:
            self.batchInputFolderLineEdit.text = folderPath
            self._checkCanApply()


    def onBrowseBatchOutputFolder(self, checked=False) -> None:
        folderPath = qt.QFileDialog.getExistingDirectory(
            slicer.util.mainWindow(),
            "Select batch output folder",
            self.batchOutputFolderLineEdit.text,
        )

        if folderPath:
            self.batchOutputFolderLineEdit.text = folderPath
            self._checkCanApply()


    def isBatchModeEnabled(self) -> bool:
        return hasattr(self, "batchModeCheckBox") and self.batchModeCheckBox.checked
        
    def cleanup(self) -> None:
        self.removeObservers()

    def enter(self) -> None:
        self.initializeParameterNode()

    def exit(self) -> None:
        if self._parameterNode:
            if self._parameterNodeGuiTag:
                self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
                self._parameterNodeGuiTag = None
            self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

    def setupGuiDefaults(self) -> None:
        """Initialize GUI defaults."""

        self.ui.scopeComboBox.clear()
        self.ui.scopeComboBox.addItem("Visible segments", "VISIBLE_SEGMENTS")
        self.ui.scopeComboBox.addItem("All segments", "ALL_SEGMENTS")

        # Default: only Joint Taubin enabled.
        # It is generally safer for multi-segment smoothing.
        self.ui.medianCheckBox.checked = False
        self.ui.openingCheckBox.checked = False
        self.ui.closingCheckBox.checked = False
        self.ui.gaussianCheckBox.checked = False
        self.ui.jointTaubinCheckBox.checked = True

        self.ui.medianKernelSizeSliderWidget.value = 3.0
        self.ui.openingKernelSizeSliderWidget.value = 3.0
        self.ui.closingKernelSizeSliderWidget.value = 3.0
        self.ui.gaussianStdSliderWidget.value = 1.0
        self.ui.jointTaubinSliderWidget.value = 0.5

        self.ui.overwriteInputCheckBox.checked = False

        if hasattr(self.ui, "statusLabel"):
            self.ui.statusLabel.text = "Ready."

        self.updateTabsFromCheckboxes()
        self.updateOutputVisibility()

    def setupConnections(self) -> None:
        """Connect GUI events."""

        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)

        self.ui.inputSegmentationSelector.connect(
            "currentNodeChanged(vtkMRMLNode*)", self._checkCanApply
        )
        self.ui.referenceVolumeSelector.connect(
            "currentNodeChanged(vtkMRMLNode*)", self._checkCanApply
        )
        self.ui.outputSegmentationSelector.connect(
            "currentNodeChanged(vtkMRMLNode*)", self._checkCanApply
        )

        self.ui.scopeComboBox.connect("currentIndexChanged(int)", self._checkCanApply)

        self.ui.medianCheckBox.connect("toggled(bool)", self.onMethodSelectionChanged)
        self.ui.openingCheckBox.connect("toggled(bool)", self.onMethodSelectionChanged)
        self.ui.closingCheckBox.connect("toggled(bool)", self.onMethodSelectionChanged)
        self.ui.gaussianCheckBox.connect("toggled(bool)", self.onMethodSelectionChanged)
        self.ui.jointTaubinCheckBox.connect("toggled(bool)", self.onMethodSelectionChanged)

        self.ui.overwriteInputCheckBox.connect("toggled(bool)", self.updateOutputVisibility)
        self.ui.overwriteInputCheckBox.connect("toggled(bool)", self._checkCanApply)

    def onSceneStartClose(self, caller, event) -> None:
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event) -> None:
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self) -> None:
        """Ensure parameter node exists and select reasonable defaults."""

        self.setParameterNode(self.logic.getParameterNode())

        if not self._parameterNode:
            return

        if not self._parameterNode.inputSegmentation:
            firstSegmentationNode = slicer.mrmlScene.GetFirstNodeByClass(
                "vtkMRMLSegmentationNode"
            )
            if firstSegmentationNode:
                self._parameterNode.inputSegmentation = firstSegmentationNode

        if not self._parameterNode.referenceVolume:
            firstVolumeNode = slicer.mrmlScene.GetFirstNodeByClass(
                "vtkMRMLScalarVolumeNode"
            )
            if firstVolumeNode:
                self._parameterNode.referenceVolume = firstVolumeNode

    def setParameterNode(self, inputParameterNode: SmoothingParameterNode | None) -> None:
        """Set and observe the parameter node."""

        if self._parameterNode:
            if self._parameterNodeGuiTag:
                self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
                self._parameterNodeGuiTag = None
            self.removeObserver(
                self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply
            )

        self._parameterNode = inputParameterNode

        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(
                self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply
            )

        self._checkCanApply()

    def currentComboData(self, comboBox):
        return comboBox.itemData(comboBox.currentIndex)

    def onMethodSelectionChanged(self, checked=False) -> None:
        """Update tab availability and Apply button state when smoothing methods change."""

        self.updateTabsFromCheckboxes()
        self.updateOutputVisibility()
        self._checkCanApply()

    def updateTabsFromCheckboxes(self) -> None:
        """Enable only the parameter tabs corresponding to selected methods."""

        tabWidget = self.ui.parametersTabWidget

        methodTabs = [
            (self.ui.medianTab, self.ui.medianCheckBox.checked),
            (self.ui.openingTab, self.ui.openingCheckBox.checked),
            (self.ui.closingTab, self.ui.closingCheckBox.checked),
            (self.ui.gaussianTab, self.ui.gaussianCheckBox.checked),
            (self.ui.jointTaubinTab, self.ui.jointTaubinCheckBox.checked),
        ]

        firstEnabledIndex = -1

        for tab, enabled in methodTabs:
            index = tabWidget.indexOf(tab)
            if index >= 0:
                tabWidget.setTabEnabled(index, enabled)
                if enabled and firstEnabledIndex < 0:
                    firstEnabledIndex = index

        if firstEnabledIndex >= 0 and not tabWidget.isTabEnabled(tabWidget.currentIndex):
            tabWidget.setCurrentIndex(firstEnabledIndex)

    def updateOutputVisibility(self, *args) -> None:
        """
        Hide output selector only when overwrite is enabled and only one method is selected.

        In batch mode, the output segmentation selector is not used because outputs
        are saved directly to the selected output folder.
        """

        if self.isBatchModeEnabled():
            showOutputSelector = False
        else:
            overwrite = self.ui.overwriteInputCheckBox.checked
            numberOfMethods = len(self.getSelectedSmoothingSteps())
            showOutputSelector = not overwrite or numberOfMethods > 1

        if hasattr(self.ui, "outputSegmentationLabel"):
            self.ui.outputSegmentationLabel.visible = showOutputSelector

        self.ui.outputSegmentationSelector.visible = showOutputSelector

    def anySmoothingMethodSelected(self) -> bool:
        """Return True if at least one smoothing method is selected."""

        return (
            self.ui.medianCheckBox.checked
            or self.ui.openingCheckBox.checked
            or self.ui.closingCheckBox.checked
            or self.ui.gaussianCheckBox.checked
            or self.ui.jointTaubinCheckBox.checked
        )

    def getSelectedSmoothingSteps(self):
        """
        Build the smoothing pipeline from selected checkboxes.

        The current fixed order is:
        Median -> Opening -> Closing -> Gaussian -> Joint Taubin.
        """

        steps = []

        if self.ui.medianCheckBox.checked:
            steps.append(
                {
                    "method": "MEDIAN",
                    "name": "Median",
                    "kernelSizeMm": self.ui.medianKernelSizeSliderWidget.value,
                }
            )

        if self.ui.openingCheckBox.checked:
            steps.append(
                {
                    "method": "MORPHOLOGICAL_OPENING",
                    "name": "Opening",
                    "kernelSizeMm": self.ui.openingKernelSizeSliderWidget.value,
                }
            )

        if self.ui.closingCheckBox.checked:
            steps.append(
                {
                    "method": "MORPHOLOGICAL_CLOSING",
                    "name": "Closing",
                    "kernelSizeMm": self.ui.closingKernelSizeSliderWidget.value,
                }
            )

        if self.ui.gaussianCheckBox.checked:
            steps.append(
                {
                    "method": "GAUSSIAN",
                    "name": "Gaussian",
                    "gaussianStandardDeviationMm": self.ui.gaussianStdSliderWidget.value,
                }
            )

        if self.ui.jointTaubinCheckBox.checked:
            steps.append(
                {
                    "method": "JOINT_TAUBIN",
                    "name": "Joint Taubin",
                    "jointTaubinSmoothingFactor": self.ui.jointTaubinSliderWidget.value,
                }
            )

        return steps
    def anySmoothingMethodSelected(self) -> bool:
        """Return True if at least one smoothing method is selected."""

        return (
            self.ui.medianCheckBox.checked
            or self.ui.openingCheckBox.checked
            or self.ui.closingCheckBox.checked
            or self.ui.gaussianCheckBox.checked
            or self.ui.jointTaubinCheckBox.checked
        )
    def _checkCanApply(self, caller=None, event=None) -> None:
        smoothingSteps = self.getSelectedSmoothingSteps()
        hasMethod = len(smoothingSteps) > 0

        if self.isBatchModeEnabled():
            inputFolder = self.batchInputFolderLineEdit.text.strip()
            outputFolder = self.batchOutputFolderLineEdit.text.strip()

            canApply = (
                hasMethod
                and os.path.isdir(inputFolder)
                and len(outputFolder) > 0
            )

            self.ui.applyButton.enabled = canApply

            if canApply:
                self.ui.applyButton.toolTip = _(
                    "Batch process all matched volume/segmentation pairs in the selected folder."
                )
                if hasattr(self.ui, "statusLabel"):
                    self.ui.statusLabel.text = "Ready to batch process segmentations."
            else:
                self.ui.applyButton.toolTip = _(
                    "Select a valid input folder, output folder, and at least one smoothing method."
                )
                if hasattr(self.ui, "statusLabel"):
                    self.ui.statusLabel.text = (
                        "Select batch input folder, output folder, and smoothing methods."
                    )

            return

        inputSegmentation = self.ui.inputSegmentationSelector.currentNode()
        referenceVolume = self.ui.referenceVolumeSelector.currentNode()
        overwriteInput = self.ui.overwriteInputCheckBox.checked
        outputSegmentation = self.ui.outputSegmentationSelector.currentNode()

        multipleMethods = len(smoothingSteps) > 1

        canApply = (
            inputSegmentation is not None
            and referenceVolume is not None
            and hasMethod
        )

        if overwriteInput and multipleMethods:
            canApply = False

        if not overwriteInput:
            canApply = canApply and outputSegmentation is not None

        self.ui.applyButton.enabled = canApply

        if canApply:
            self.ui.applyButton.toolTip = _(
                "Apply each selected smoothing method independently to the original segmentation."
            )
            if hasattr(self.ui, "statusLabel"):
                self.ui.statusLabel.text = "Ready to apply smoothing."
        else:
            if overwriteInput and multipleMethods:
                self.ui.applyButton.toolTip = _(
                    "Overwrite input is only allowed when one smoothing method is selected."
                )
                if hasattr(self.ui, "statusLabel"):
                    self.ui.statusLabel.text = (
                        "Disable overwrite or select only one smoothing method."
                    )
            else:
                self.ui.applyButton.toolTip = _(
                    "Select input segmentation, reference volume, output segmentation, and at least one smoothing method."
                )
                if hasattr(self.ui, "statusLabel"):
                    self.ui.statusLabel.text = (
                        "Select the required inputs and smoothing methods."
                    )
                    
    def onApplyButton(self) -> None:
        """
        Run smoothing when the user clicks Apply.

        In normal mode:
            Apply selected smoothing methods to the selected input segmentation.

        In batch mode:
            Load all matched Volume_XXX / Segmentation_XXX pairs from a folder,
            apply selected smoothing methods independently, and save the outputs.
        """

        with slicer.util.tryWithErrorDisplay(
            _("Failed to apply smoothing."), waitCursor=True
        ):

            self.resetProgressBars()

            scope = self.currentComboData(self.ui.scopeComboBox)
            smoothingSteps = self.getSelectedSmoothingSteps()

            if not smoothingSteps:
                raise ValueError("At least one smoothing method must be selected.")

            # ------------------------------------------------------------
            # Batch mode
            # ------------------------------------------------------------
            if self.isBatchModeEnabled():
                inputFolder = self.batchInputFolderLineEdit.text.strip()
                outputFolder = self.batchOutputFolderLineEdit.text.strip()
                recursive = self.batchRecursiveCheckBox.checked
                keepLoadedNodes = self.batchKeepLoadedNodesCheckBox.checked

                if not os.path.isdir(inputFolder):
                    raise ValueError(f"Batch input folder does not exist: {inputFolder}")

                if not outputFolder:
                    raise ValueError("Batch output folder is empty.")

                self.updateBatchProgress(0, "Batch smoothing started...")

                summary = self.logic.batchSmoothSegmentations(
                    inputFolder=inputFolder,
                    outputFolder=outputFolder,
                    steps=smoothingSteps,
                    scope=scope,
                    recursive=recursive,
                    keepLoadedNodes=keepLoadedNodes,
                    progressCallback=self.updateBatchProgress,
                )

                self.updateBatchProgress(
                    100,
                    (
                        f"Batch completed. Processed {summary['processed_pairs']} pair(s), "
                        f"saved {summary['saved_outputs']} output file(s), "
                        f"failed {len(summary['failed_pairs'])} pair(s)."
                    )
                )

                failureText = ""

                if len(summary["failed_pairs"]) > 0:
                    failureLines = []

                    for failedPair in summary["failed_pairs"]:
                        failureLines.append(
                            f"- Sample {failedPair['sampleId']}: {failedPair['error']}"
                        )

                    failureText = "\n\nFailures:\n" + "\n".join(failureLines)

                slicer.util.infoDisplay(
                    f"Batch smoothing completed.\n\n"
                    f"Found pairs: {summary['found_pairs']}\n"
                    f"Processed pairs: {summary['processed_pairs']}\n"
                    f"Saved outputs: {summary['saved_outputs']}\n"
                    f"Failed pairs: {len(summary['failed_pairs'])}\n"
                    f"Output folder:\n{outputFolder}"
                    f"{failureText}"
                )

                return

            # ------------------------------------------------------------
            # Single segmentation mode
            # ------------------------------------------------------------
            inputSegmentation = self.ui.inputSegmentationSelector.currentNode()
            referenceVolume = self.ui.referenceVolumeSelector.currentNode()
            overwriteInput = self.ui.overwriteInputCheckBox.checked

            if overwriteInput and len(smoothingSteps) > 1:
                raise ValueError(
                    "Overwrite input can only be used when one smoothing method is selected. "
                    "Disable overwrite to generate one independent output segmentation per method."
                )


            if overwriteInput:
                step = smoothingSteps[0]
                methodName = step.get("name", step["method"])


                self.logic.smoothSegmentation(
                    segmentationNode=inputSegmentation,
                    referenceVolumeNode=referenceVolume,
                    method=step["method"],
                    scope=scope,
                    kernelSizeMm=step.get("kernelSizeMm", 3.0),
                    gaussianStandardDeviationMm=step.get(
                        "gaussianStandardDeviationMm", 1.0
                    ),
                    jointTaubinSmoothingFactor=step.get(
                        "jointTaubinSmoothingFactor", 0.5
                    ),
                )

                outputNodes = [inputSegmentation]

            else:
                outputNodes = self.logic.smoothSegmentationIndependently(
                    inputSegmentationNode=inputSegmentation,
                    referenceVolumeNode=referenceVolume,
                    steps=smoothingSteps,
                    scope=scope,
                    baseOutputSegmentationNode=self.ui.outputSegmentationSelector.currentNode(),
                )

            slicer.util.infoDisplay(
                f"Smoothing completed. Created {len(outputNodes)} output segmentation(s)."
            )

#
# SmoothingLogic
#


class SmoothingLogic(ScriptedLoadableModuleLogic):
    """Computation logic for segmentation smoothing."""

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)

    def loadSegmentationNodeRobust(self, segmentationPath, referenceVolumeNode=None):
        """
        Load a segmentation file robustly.

        First tries slicer.util.loadSegmentation().
        If that fails, tries to load the file as a labelmap volume and convert it
        to a segmentation node.

        This is needed because .nrrd files may be either:
            - true Slicer segmentation files: .seg.nrrd
            - labelmap volumes: .nrrd
        """

        logging.info(f"[BATCH LOAD] Trying to load segmentation: {segmentationPath}")

        try:
            segmentationNode = slicer.util.loadSegmentation(segmentationPath)
        except Exception as exc:
            logging.warning(
                f"[BATCH LOAD] loadSegmentation raised an exception: {str(exc)}"
            )
            segmentationNode = None

        if segmentationNode is not None:
            logging.info(
                f"[BATCH LOAD] Loaded as segmentation node: {segmentationNode.GetName()}"
            )
            return segmentationNode

        logging.warning(
            f"[BATCH LOAD] loadSegmentation failed. Trying as labelmap volume: {segmentationPath}"
        )

        try:
            labelmapNode = slicer.util.loadLabelVolume(segmentationPath)
        except Exception as exc:
            logging.warning(
                f"[BATCH LOAD] loadLabelVolume raised an exception: {str(exc)}"
            )
            labelmapNode = None

        if labelmapNode is None:
            raise RuntimeError(
                f"Failed to load segmentation as .seg.nrrd or labelmap: {segmentationPath}"
            )

        segmentationNode = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationNode",
            os.path.splitext(os.path.basename(segmentationPath))[0] + "_Segmentation",
        )

        segmentationNode.CreateDefaultDisplayNodes()

        if referenceVolumeNode is not None:
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(referenceVolumeNode)

        slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(
            labelmapNode,
            segmentationNode,
        )

        slicer.mrmlScene.RemoveNode(labelmapNode)

        if segmentationNode.GetSegmentation().GetNumberOfSegments() == 0:
            raise RuntimeError(
                f"Loaded labelmap but no segments were imported: {segmentationPath}"
            )

        logging.info(
            f"[BATCH LOAD] Loaded labelmap and converted to segmentation: "
            f"{segmentationNode.GetName()} with "
            f"{segmentationNode.GetSegmentation().GetNumberOfSegments()} segment(s)"
        )

        return segmentationNode
    
    def getParameterNode(self):
        return SmoothingParameterNode(super().getParameterNode())

    def copySegmentationContent(self, inputSegmentationNode, outputSegmentationNode, outputName=None):
        """
        Copy only segmentation content and force an independent display node.

        This avoids shared visibility/display behavior between segmentation nodes.
        """

        if inputSegmentationNode is None or outputSegmentationNode is None:
            raise ValueError("Input or output segmentation node is invalid.")

        if outputName:
            outputSegmentationNode.SetName(outputName)

        # ------------------------------------------------------------------
        # 1) Remove any previous display node references from the output node
        # ------------------------------------------------------------------
        outputSegmentationNode.RemoveAllDisplayNodeIDs()

        # ------------------------------------------------------------------
        # 2) Clear old segmentation content
        # ------------------------------------------------------------------
        outputSegmentationNode.GetSegmentation().RemoveAllSegments()

        # ------------------------------------------------------------------
        # 3) Deep-copy only the internal vtkSegmentation content
        # ------------------------------------------------------------------
        outputSegmentationNode.GetSegmentation().DeepCopy(
            inputSegmentationNode.GetSegmentation()
        )

        # ------------------------------------------------------------------
        # 4) Create a completely new independent display node
        # ------------------------------------------------------------------
        outputDisplayNode = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentationDisplayNode",
            outputSegmentationNode.GetName() + "_Display"
        )

        outputSegmentationNode.SetAndObserveDisplayNodeID(outputDisplayNode.GetID())

        # ------------------------------------------------------------------
        # 5) Copy transform if input segmentation has one
        # ------------------------------------------------------------------
        outputSegmentationNode.SetAndObserveTransformNodeID(
            inputSegmentationNode.GetTransformNodeID()
        )

        # ------------------------------------------------------------------
        # 6) Optional: make output visible
        # ------------------------------------------------------------------
        outputDisplayNode.SetVisibility(True)
        outputDisplayNode.SetVisibility3D(True)
        outputDisplayNode.SetVisibility2DFill(True)
        outputDisplayNode.SetVisibility2DOutline(True)
    def cloneSegmentation(self, inputSegmentationNode, outputSegmentationNode) -> None:
        """Copy input segmentation content into the output segmentation node."""

        self.copySegmentationContent(
            inputSegmentationNode=inputSegmentationNode,
            outputSegmentationNode=outputSegmentationNode,
            outputName=inputSegmentationNode.GetName() + "_smoothed",
        )

    def smoothSegmentationPipeline(
        self,
        segmentationNode,
        referenceVolumeNode,
        steps,
        scope="VISIBLE_SEGMENTS",
    ) -> None:
        """Apply multiple smoothing steps sequentially."""

        if not steps:
            raise ValueError("No smoothing steps were selected.")

        startTime = time.time()
        logging.info("Segmentation smoothing pipeline started")

        for stepIndex, step in enumerate(steps, start=1):
            logging.info(
                f"Applying smoothing step {stepIndex}/{len(steps)}: {step.get('name', step.get('method'))}"
            )

            self.smoothSegmentation(
                segmentationNode=segmentationNode,
                referenceVolumeNode=referenceVolumeNode,
                method=step["method"],
                scope=scope,
                kernelSizeMm=step.get("kernelSizeMm", 3.0),
                gaussianStandardDeviationMm=step.get(
                    "gaussianStandardDeviationMm", 1.0
                ),
                jointTaubinSmoothingFactor=step.get(
                    "jointTaubinSmoothingFactor", 0.5
                ),
            )

        stopTime = time.time()
        logging.info(
            f"Segmentation smoothing pipeline completed in {stopTime - startTime:.2f} seconds"
        )

    def smoothSegmentation(
        self,
        segmentationNode,
        referenceVolumeNode,
        method="JOINT_TAUBIN",
        scope="VISIBLE_SEGMENTS",
        kernelSizeMm=3.0,
        gaussianStandardDeviationMm=1.0,
        jointTaubinSmoothingFactor=0.5,
    ) -> None:
        """Apply one Slicer Segment Editor Smoothing effect to a segmentation."""

        if segmentationNode is None:
            raise ValueError("Segmentation node is invalid.")

        if referenceVolumeNode is None:
            raise ValueError("Reference volume node is invalid.")

        startTime = time.time()
        logging.info(f"Segmentation smoothing started: {method}")

        # Make sure binary labelmap representation exists.
        segmentationNode.GetSegmentation().CreateRepresentation(
            slicer.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        )

        segmentEditorWidget = slicer.qMRMLSegmentEditorWidget()
        segmentEditorWidget.setMRMLScene(slicer.mrmlScene)

        segmentEditorNode = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLSegmentEditorNode"
        )
        segmentEditorWidget.setMRMLSegmentEditorNode(segmentEditorNode)

        segmentEditorWidget.setSegmentationNode(segmentationNode)
        segmentEditorWidget.setSourceVolumeNode(referenceVolumeNode)

        originalVisibleSegmentIds = self.visibleSegmentIds(segmentationNode)

        try:
            if scope == "ALL_SEGMENTS":
                self.setAllSegmentsVisible(segmentationNode, True)

            segmentEditorWidget.setActiveEffectByName("Smoothing")
            effect = segmentEditorWidget.activeEffect()

            if effect is None:
                raise RuntimeError("Could not activate Segment Editor Smoothing effect.")

            effect.setParameter("SmoothingMethod", method)

            # Apply to all visible segments.
            # For ALL_SEGMENTS, all segments are temporarily made visible above.
            effect.setParameter("ApplyToAllVisibleSegments", "1")

            if method in [
                "MEDIAN",
                "MORPHOLOGICAL_OPENING",
                "MORPHOLOGICAL_CLOSING",
            ]:
                effect.setParameter("KernelSizeMm", str(kernelSizeMm))

            elif method == "GAUSSIAN":
                effect.setParameter(
                    "GaussianStandardDeviationMm",
                    str(gaussianStandardDeviationMm),
                )

            elif method == "JOINT_TAUBIN":
                effect.setParameter(
                    "JointTaubinSmoothingFactor",
                    str(jointTaubinSmoothingFactor),
                )

            else:
                raise ValueError(f"Unsupported smoothing method: {method}")

            effect.self().onApply()

        finally:
            if scope == "ALL_SEGMENTS":
                self.restoreVisibleSegments(segmentationNode, originalVisibleSegmentIds)

            segmentEditorWidget.setMRMLSegmentEditorNode(None)
            slicer.mrmlScene.RemoveNode(segmentEditorNode)
            segmentEditorWidget = None

        stopTime = time.time()
        logging.info(
            f"Segmentation smoothing step completed in {stopTime - startTime:.2f} seconds"
        )
    def safeNodeName(self, name: str) -> str:
        """Return a compact name fragment suitable for MRML node names."""

        return (
            name.strip()
            .replace(" ", "_")
            .replace("/", "_")
            .replace("\\", "_")
            .replace("(", "")
            .replace(")", "")
        )
    def smoothSegmentationIndependently(
        self,
        inputSegmentationNode,
        referenceVolumeNode,
        steps,
        scope="VISIBLE_SEGMENTS",
        baseOutputSegmentationNode=None,
    ):
        """
        Apply each smoothing method independently to the original segmentation.

        For each selected method:
        1. Create a fresh copy of the original segmentation.
        2. Apply exactly one smoothing method to that copy.
        3. Keep the result as an independent output segmentation.

        This intentionally does NOT apply smoothing methods sequentially.
        """

        if inputSegmentationNode is None:
            raise ValueError("Input segmentation node is invalid.")

        if referenceVolumeNode is None:
            raise ValueError("Reference volume node is invalid.")

        if not steps:
            raise ValueError("No smoothing steps were selected.")

        startTime = time.time()
        logging.info("Independent smoothing started")

        outputNodes = []

        totalSteps = len(steps)

        for stepIndex, step in enumerate(steps, start=1):
            methodName = step.get("name", step.get("method", f"Step{stepIndex}"))
            safeMethodName = self.safeNodeName(methodName)

            outputName = f"{inputSegmentationNode.GetName()}_{safeMethodName}_smoothed"

            if (
                len(steps) == 1
                and baseOutputSegmentationNode is not None
                and baseOutputSegmentationNode != inputSegmentationNode
            ):
                outputNode = baseOutputSegmentationNode
            else:
                outputNode = slicer.mrmlScene.AddNewNodeByClass(
                    "vtkMRMLSegmentationNode",
                    outputName,
                )

            self.copySegmentationContent(
                inputSegmentationNode=inputSegmentationNode,
                outputSegmentationNode=outputNode,
                outputName=outputName,
            )

            logging.info(
                f"Applying independent smoothing {stepIndex}/{len(steps)}: {methodName}"
            )

            self.smoothSegmentation(
                segmentationNode=outputNode,
                referenceVolumeNode=referenceVolumeNode,
                method=step["method"],
                scope=scope,
                kernelSizeMm=step.get("kernelSizeMm", 3.0),
                gaussianStandardDeviationMm=step.get(
                    "gaussianStandardDeviationMm", 1.0
                ),
                jointTaubinSmoothingFactor=step.get(
                    "jointTaubinSmoothingFactor", 0.5
                ),
            )

            outputNodes.append(outputNode)

        stopTime = time.time()
        logging.info(
            f"Independent smoothing completed in {stopTime - startTime:.2f} seconds"
        )

        return outputNodes

    def smoothSegmentationPipeline(
        self,
        segmentationNode,
        referenceVolumeNode,
        steps,
        scope="VISIBLE_SEGMENTS",
    ) -> None:
        """Apply multiple smoothing steps sequentially."""

        if segmentationNode is None:
            raise ValueError("Segmentation node is invalid.")

        if referenceVolumeNode is None:
            raise ValueError("Reference volume node is invalid.")

        if not steps:
            raise ValueError("No smoothing steps were selected.")

        startTime = time.time()
        logging.info("Segmentation smoothing pipeline started")

        for stepIndex, step in enumerate(steps, start=1):
            logging.info(
                f"Applying smoothing step {stepIndex}/{len(steps)}: "
                f"{step.get('name', step.get('method'))}"
            )

            self.smoothSegmentation(
                segmentationNode=segmentationNode,
                referenceVolumeNode=referenceVolumeNode,
                method=step["method"],
                scope=scope,
                kernelSizeMm=step.get("kernelSizeMm", 3.0),
                gaussianStandardDeviationMm=step.get(
                    "gaussianStandardDeviationMm", 1.0
                ),
                jointTaubinSmoothingFactor=step.get(
                    "jointTaubinSmoothingFactor", 0.5
                ),
            )

        stopTime = time.time()
        logging.info(
            f"Segmentation smoothing pipeline completed in {stopTime - startTime:.2f} seconds"
        )

    def visibleSegmentIds(self, segmentationNode):
        """Return list of currently visible segment IDs."""

        displayNode = segmentationNode.GetDisplayNode()
        if displayNode is None:
            segmentationNode.CreateDefaultDisplayNodes()
            displayNode = segmentationNode.GetDisplayNode()

        segmentation = segmentationNode.GetSegmentation()
        segmentIds = vtk.vtkStringArray()
        segmentation.GetSegmentIDs(segmentIds)

        visibleIds = []
        for i in range(segmentIds.GetNumberOfValues()):
            segmentId = segmentIds.GetValue(i)
            if displayNode.GetSegmentVisibility(segmentId):
                visibleIds.append(segmentId)

        return visibleIds

    def setAllSegmentsVisible(self, segmentationNode, visible=True) -> None:
        """Set visibility for all segments."""

        displayNode = segmentationNode.GetDisplayNode()
        if displayNode is None:
            segmentationNode.CreateDefaultDisplayNodes()
            displayNode = segmentationNode.GetDisplayNode()

        segmentation = segmentationNode.GetSegmentation()
        segmentIds = vtk.vtkStringArray()
        segmentation.GetSegmentIDs(segmentIds)

        for i in range(segmentIds.GetNumberOfValues()):
            segmentId = segmentIds.GetValue(i)
            displayNode.SetSegmentVisibility(segmentId, visible)

    def restoreVisibleSegments(self, segmentationNode, visibleSegmentIds) -> None:
        """Restore segment visibility after temporary all-segment processing."""

        displayNode = segmentationNode.GetDisplayNode()
        if displayNode is None:
            segmentationNode.CreateDefaultDisplayNodes()
            displayNode = segmentationNode.GetDisplayNode()

        segmentation = segmentationNode.GetSegmentation()
        segmentIds = vtk.vtkStringArray()
        segmentation.GetSegmentIDs(segmentIds)

        visibleSegmentIds = set(visibleSegmentIds)

        for i in range(segmentIds.GetNumberOfValues()):
            segmentId = segmentIds.GetValue(i)
            displayNode.SetSegmentVisibility(
                segmentId, segmentId in visibleSegmentIds
            )
    
    def isVolumeFile(self, filePath):
        fileName = os.path.basename(filePath).lower()

        volumeExtensions = [
            ".nii",
            ".nii.gz",
            ".nrrd",
            ".nhdr",
            ".mha",
            ".mhd",
        ]

        if self.isSegmentationFile(filePath):
            return False

        return any(fileName.endswith(ext) for ext in volumeExtensions)


    def isSegmentationFile(self, filePath):
        fileName = os.path.basename(filePath).lower()

        segmentationExtensions = [
            ".seg.nrrd",
            ".seg.nhdr",
            ".seg.vtm",
            ".seg.vtk",
            ".nrrd",
            ".nii",
            ".nii.gz",
            ".mha",
            ".mhd",
        ]

        if ".seg." in fileName:
            return True

        segmentationKeywords = [
            "seg",
            "segmentation",
            "label",
            "labels",
            "mask",
        ]

        hasSegmentationKeyword = any(
            keyword in fileName for keyword in segmentationKeywords
        )

        hasSupportedExtension = any(
            fileName.endswith(ext) for ext in segmentationExtensions
        )

        return hasSegmentationKeyword and hasSupportedExtension


    def sampleIdFromFileName(self, filePath):
        """
        Extract sample ID from names like:

            Volume_001.nrrd
            Volume_001.nii.gz
            Segmentation_001.seg.nrrd
            Segmentation_001.nrrd

        Returns:
            "001"

        If the file name does not match this pattern, returns None.
        """

        fileName = os.path.basename(filePath)

        match = re.search(
            r"^(Volume|Segmentation)_(.+?)(\.seg)?"
            r"(\.nii\.gz|\.nii|\.nrrd|\.nhdr|\.mha|\.mhd|\.vtk|\.vtm)$",
            fileName,
            flags=re.IGNORECASE,
        )

        if not match:
            return None

        return match.group(2)


    def collectFilesFromFolder(self, folderPath, recursive=True):
        """Collect all files from a folder."""

        allFiles = []

        if recursive:
            for root, dirs, files in os.walk(folderPath):
                for fileName in files:
                    allFiles.append(os.path.join(root, fileName))
        else:
            for fileName in os.listdir(folderPath):
                filePath = os.path.join(folderPath, fileName)
                if os.path.isfile(filePath):
                    allFiles.append(filePath)

        return allFiles


    def findVolumeSegmentationPairs(self, folderPath, recursive=True):
        """
        Find matching volume/segmentation pairs in a folder.

        Matching rule:
            Volume_XXX.ext
            Segmentation_XXX.ext
        """

        if not os.path.isdir(folderPath):
            raise ValueError(f"Folder does not exist: {folderPath}")

        allFiles = self.collectFilesFromFolder(folderPath, recursive=recursive)

        logging.info(f"[BATCH PAIRING] Folder: {folderPath}")
        logging.info(f"[BATCH PAIRING] Recursive: {recursive}")
        logging.info(f"[BATCH PAIRING] Total files found: {len(allFiles)}")

        volumeFiles = [
            filePath for filePath in allFiles
            if self.isVolumeFile(filePath)
        ]

        segmentationFiles = [
            filePath for filePath in allFiles
            if self.isSegmentationFile(filePath)
        ]

        logging.info(f"[BATCH PAIRING] Candidate volume files: {len(volumeFiles)}")
        for filePath in volumeFiles:
            logging.info(f"[BATCH PAIRING] Volume candidate: {os.path.basename(filePath)}")

        logging.info(f"[BATCH PAIRING] Candidate segmentation files: {len(segmentationFiles)}")
        for filePath in segmentationFiles:
            logging.info(f"[BATCH PAIRING] Segmentation candidate: {os.path.basename(filePath)}")

        segmentationBySampleId = {}

        for segmentationFile in segmentationFiles:
            sampleId = self.sampleIdFromFileName(segmentationFile)

            logging.info(
                f"[BATCH PAIRING] Segmentation sample ID: "
                f"{os.path.basename(segmentationFile)} -> {sampleId}"
            )

            if sampleId is not None:
                segmentationBySampleId.setdefault(sampleId, []).append(segmentationFile)

        pairs = []

        for volumeFile in volumeFiles:
            sampleId = self.sampleIdFromFileName(volumeFile)

            logging.info(
                f"[BATCH PAIRING] Volume sample ID: "
                f"{os.path.basename(volumeFile)} -> {sampleId}"
            )

            if sampleId is None:
                continue

            if sampleId in segmentationBySampleId:
                for segmentationFile in segmentationBySampleId[sampleId]:
                    pairs.append(
                        {
                            "sampleId": sampleId,
                            "volumePath": volumeFile,
                            "segmentationPath": segmentationFile,
                        }
                    )

                    logging.info(
                        f"[BATCH PAIRING] Pair found: "
                        f"{os.path.basename(volumeFile)} + "
                        f"{os.path.basename(segmentationFile)}"
                    )

        logging.info(f"[BATCH PAIRING] Total pairs found: {len(pairs)}")

        return pairs

    def batchSmoothSegmentations(
        self,
        inputFolder,
        outputFolder,
        steps,
        scope="ALL_SEGMENTS",
        recursive=True,
        keepLoadedNodes=False,
        progressCallback=None,
    ):
        """
        Batch process segmentation files from a folder.

        For each matched pair:
            Volume_XXX.ext
            Segmentation_XXX.ext

        The function:
            1. Loads the volume.
            2. Loads the segmentation.
            3. Applies each selected smoothing method independently.
            4. Saves each output segmentation as .seg.nrrd.
            5. Removes temporary nodes unless keepLoadedNodes=True.
        """

        if not os.path.isdir(inputFolder):
            raise ValueError(f"Input folder does not exist: {inputFolder}")

        if not steps:
            raise ValueError("No smoothing steps were selected.")

        os.makedirs(outputFolder, exist_ok=True)

        inputFolderAbs = os.path.abspath(inputFolder)
        outputFolderAbs = os.path.abspath(outputFolder)

        if outputFolderAbs == inputFolderAbs:
            raise ValueError(
                "The batch output folder cannot be the same as the input folder. "
                "Select a separate output folder."
            )

        if recursive and outputFolderAbs.startswith(inputFolderAbs + os.sep):
            raise ValueError(
                "The batch output folder cannot be inside the input folder when recursive search is enabled. "
                "Select a separate output folder or disable recursive search."
            )

        pairs = self.findVolumeSegmentationPairs(
            folderPath=inputFolder,
            recursive=recursive,
        )

        if not pairs:
            raise ValueError(
                "No matching volume/segmentation pairs were found. "
                "Expected names such as Volume_001.nrrd and Segmentation_001.seg.nrrd."
            )

        processedPairs = 0
        savedOutputs = 0
        failedPairs = []

        totalPairs = len(pairs)
        totalOperations = totalPairs * len(steps)
        completedOperations = 0

        if progressCallback:
            progressCallback(
                0,
                f"Batch smoothing started. Found {totalPairs} pair(s)."
            )

        logging.info(f"Batch smoothing started. Found {totalPairs} pair(s).")

        for pairIndex, pair in enumerate(pairs, start=1):
            sampleId = pair["sampleId"]
            volumePath = pair["volumePath"]
            segmentationPath = pair["segmentationPath"]

            logging.info(
                f"Processing pair {pairIndex}/{totalPairs}: "
                f"sample={sampleId}, "
                f"volume={os.path.basename(volumePath)}, "
                f"segmentation={os.path.basename(segmentationPath)}"
            )

            if progressCallback:
                progressValue = int((completedOperations / totalOperations) * 100)
                progressCallback(
                    progressValue,
                    f"Loading sample {sampleId} ({pairIndex}/{totalPairs})..."
                )

            loadedNodes = []
            outputNodes = []

            try:
                logging.info(f"[BATCH LOAD] Loading volume: {volumePath}")

                referenceVolumeNode = slicer.util.loadVolume(volumePath)

                if referenceVolumeNode is None:
                    raise RuntimeError(f"Failed to load volume: {volumePath}")

                logging.info(f"[BATCH LOAD] Loaded volume node: {referenceVolumeNode.GetName()}")

                loadedNodes.append(referenceVolumeNode)

                segmentationNode = self.loadSegmentationNodeRobust(
                    segmentationPath=segmentationPath,
                    referenceVolumeNode=referenceVolumeNode,
                )

                if segmentationNode is None:
                    raise RuntimeError(f"Failed to load segmentation: {segmentationPath}")

                segmentationNode.CreateDefaultDisplayNodes()

                logging.info(
                    f"[BATCH LOAD] Loaded segmentation node: {segmentationNode.GetName()} "
                    f"with {segmentationNode.GetSegmentation().GetNumberOfSegments()} segment(s)"
                )

                loadedNodes.append(segmentationNode)

                if segmentationNode.GetSegmentation().GetNumberOfSegments() == 0:
                    raise RuntimeError(
                        f"Segmentation has no segments: {segmentationPath}"
                    )

                for stepIndex, step in enumerate(steps, start=1):
                    methodName = step.get("name", step.get("method", f"Step{stepIndex}"))
                    safeMethodName = self.safeNodeName(methodName)

                    if progressCallback:
                        progressValue = int((completedOperations / totalOperations) * 100)
                        progressCallback(
                            progressValue,
                            (
                                f"Sample {sampleId}: applying {methodName} "
                                f"({stepIndex}/{len(steps)}), "
                                f"case {pairIndex}/{totalPairs}..."
                            )
                        )

                    outputName = (
                        f"Segmentation_{sampleId}_{safeMethodName}_smoothed"
                    )

                    outputNode = slicer.mrmlScene.AddNewNodeByClass(
                        "vtkMRMLSegmentationNode",
                        outputName,
                    )

                    self.copySegmentationContent(
                        inputSegmentationNode=segmentationNode,
                        outputSegmentationNode=outputNode,
                        outputName=outputName,
                    )

                    self.smoothSegmentation(
                        segmentationNode=outputNode,
                        referenceVolumeNode=referenceVolumeNode,
                        method=step["method"],
                        scope=scope,
                        kernelSizeMm=step.get("kernelSizeMm", 3.0),
                        gaussianStandardDeviationMm=step.get(
                            "gaussianStandardDeviationMm", 1.0
                        ),
                        jointTaubinSmoothingFactor=step.get(
                            "jointTaubinSmoothingFactor", 0.5
                        ),
                    )

                    outputNodes.append(outputNode)

                    outputFileName = (
                        f"Segmentation_{sampleId}_{safeMethodName}_smoothed.seg.nrrd"
                    )

                    outputPath = os.path.join(outputFolder, outputFileName)

                    success = slicer.util.saveNode(outputNode, outputPath)

                    if not success:
                        raise RuntimeError(f"Failed to save output: {outputPath}")

                    logging.info(f"Saved: {outputPath}")
                    savedOutputs += 1

                    completedOperations += 1

                    if progressCallback:
                        progressValue = int((completedOperations / totalOperations) * 100)
                        progressCallback(
                            progressValue,
                            (
                                f"Saved {methodName} result for sample {sampleId}. "
                                f"Progress: {completedOperations}/{totalOperations}."
                            )
                        )

                processedPairs += 1

            except Exception as exc:
                logging.error(
                    f"Failed to process sample {sampleId}: {str(exc)}"
                )

                failedPairs.append(
                    {
                        "sampleId": sampleId,
                        "volumePath": volumePath,
                        "segmentationPath": segmentationPath,
                        "error": str(exc),
                    }
                )

                # Count failed sample as completed operations to avoid a stuck progress bar.
                completedOperations += len(steps)

                if completedOperations > totalOperations:
                    completedOperations = totalOperations

                if progressCallback:
                    progressValue = int((completedOperations / totalOperations) * 100)
                    progressCallback(
                        progressValue,
                        f"Failed sample {sampleId}: {str(exc)}"
                    )

            finally:
                if not keepLoadedNodes:
                    for node in outputNodes:
                        if node is not None and slicer.mrmlScene.IsNodePresent(node):
                            slicer.mrmlScene.RemoveNode(node)

                    for node in loadedNodes:
                        if node is not None and slicer.mrmlScene.IsNodePresent(node):
                            slicer.mrmlScene.RemoveNode(node)

                slicer.app.processEvents()

        logging.info(
            f"Batch smoothing completed. "
            f"Processed {processedPairs}/{totalPairs} pair(s). "
            f"Saved {savedOutputs} output file(s). "
            f"Failed {len(failedPairs)} pair(s)."
        )

        if progressCallback:
            progressCallback(
                100,
                (
                    f"Batch completed. Processed {processedPairs}/{totalPairs} pair(s), "
                    f"saved {savedOutputs} output file(s), "
                    f"failed {len(failedPairs)} pair(s)."
                )
            )

        return {
            "found_pairs": totalPairs,
            "processed_pairs": processedPairs,
            "saved_outputs": savedOutputs,
            "failed_pairs": failedPairs,
        }


#
# SmoothingTest
#
#
# SmoothingTest
#


class SmoothingTest(ScriptedLoadableModuleTest):
    """
    Test Smoothing module using one random volume/segmentation pair
    from the extension Data folder.

    Expected Data folder structure:
        Smoothing/
            Smoothing.py
            Data/
                case01_volume.nrrd
                case01_segmentation.seg.nrrd

    The test searches for volume files and segmentation files with matching
    basename fragments, then randomly selects one pair.
    """

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_LoadRandomDataAndApplyAllSmoothingMethods()

    def test_LoadRandomDataAndApplyAllSmoothingMethods(self):
        import os
        import random
        self.delayDisplay("Testing Smoothing with random data pair")

        logic = SmoothingLogic()
        self.assertIsNotNone(logic)

        # ------------------------------------------------------------
        # 1) Locate extension data folder
        # ------------------------------------------------------------
        moduleDir = os.path.dirname(os.path.abspath(__file__))

        # Smoothing.py is inside:
        #   SMOOTHING_EXTENSION/Smoothing/Smoothing.py
        #
        # The data folder is at:
        #   SMOOTHING_EXTENSION/data
        extensionRootDir = os.path.abspath(os.path.join(moduleDir, os.pardir))
        dataDir = os.path.join(extensionRootDir, "data")

        self.assertTrue(
            os.path.isdir(dataDir),
            f"Data folder not found: {dataDir}"
        )

        # ------------------------------------------------------------
        # 2) Find candidate volume and segmentation files
        # ------------------------------------------------------------
        volumeExtensions = [
            ".nii",
            ".nii.gz",
            ".nrrd",
            ".nhdr",
            ".mha",
            ".mhd",
        ]

        segmentationExtensions = [
            ".seg.nrrd",
            ".seg.nhdr",
            ".seg.vtm",
            ".seg.vtk",
            ".nrrd",
            ".nii",
            ".nii.gz",
        ]

        allFiles = []
        for root, dirs, files in os.walk(dataDir):
            for fileName in files:
                allFiles.append(os.path.join(root, fileName))

        segmentationFiles = [
            f for f in allFiles
            if self._isSegmentationFile(f, segmentationExtensions)
        ]

        volumeFiles = [
            f for f in allFiles
            if self._isVolumeFile(f, volumeExtensions)
            and not self._isSegmentationFile(f, segmentationExtensions)
        ]

        self.assertGreater(
            len(volumeFiles),
            0,
            f"No volume files found in: {dataDir}"
        )

        self.assertGreater(
            len(segmentationFiles),
            0,
            f"No segmentation files found in: {dataDir}"
        )

        # ------------------------------------------------------------
        # 3) Match volume and segmentation files by normalized basename
        # ------------------------------------------------------------
        pairs = self._findVolumeSegmentationPairs(volumeFiles, segmentationFiles)

        self.assertGreater(
            len(pairs),
            0,
            (
                "No matching volume/segmentation pairs found. "
                "Use related file names such as: "
                "'case01_volume.nrrd' and 'case01_segmentation.seg.nrrd'."
            )
        )

        volumePath, segmentationPath = random.choice(pairs)

        self.delayDisplay(f"Selected volume: {os.path.basename(volumePath)}")
        self.delayDisplay(f"Selected segmentation: {os.path.basename(segmentationPath)}")

        # ------------------------------------------------------------
        # 4) Load selected volume and segmentation
        # ------------------------------------------------------------
        referenceVolumeNode = slicer.util.loadVolume(volumePath)

        self.assertIsNotNone(
            referenceVolumeNode,
            f"Failed to load volume: {volumePath}"
        )

        segmentationNode = slicer.util.loadSegmentation(segmentationPath)

        self.assertIsNotNone(
            segmentationNode,
            f"Failed to load segmentation: {segmentationPath}"
        )

        segmentationNode.CreateDefaultDisplayNodes()

        self.assertGreater(
            segmentationNode.GetSegmentation().GetNumberOfSegments(),
            0,
            "Loaded segmentation does not contain any segments."
        )

        # ------------------------------------------------------------
        # 5) Define all smoothing algorithms with default parameters
        # ------------------------------------------------------------
        smoothingSteps = [
            {
                "method": "MEDIAN",
                "name": "Median",
                "kernelSizeMm": 3.0,
            },
            {
                "method": "MORPHOLOGICAL_OPENING",
                "name": "Opening",
                "kernelSizeMm": 3.0,
            },
            {
                "method": "MORPHOLOGICAL_CLOSING",
                "name": "Closing",
                "kernelSizeMm": 3.0,
            },
            {
                "method": "GAUSSIAN",
                "name": "Gaussian",
                "gaussianStandardDeviationMm": 1.0,
            },
            {
                "method": "JOINT_TAUBIN",
                "name": "Joint Taubin",
                "jointTaubinSmoothingFactor": 0.5,
            },
        ]

        # ------------------------------------------------------------
        # 6) Apply all algorithms independently to the original segmentation
        # ------------------------------------------------------------
        outputNodes = logic.smoothSegmentationIndependently(
            inputSegmentationNode=segmentationNode,
            referenceVolumeNode=referenceVolumeNode,
            steps=smoothingSteps,
            scope="ALL_SEGMENTS",
            baseOutputSegmentationNode=None,
        )

        self.assertEqual(
            len(outputNodes),
            len(smoothingSteps),
            "Unexpected number of output segmentations."
        )

        # ------------------------------------------------------------
        # 7) Validate outputs
        # ------------------------------------------------------------
        inputSegmentCount = segmentationNode.GetSegmentation().GetNumberOfSegments()

        for outputNode in outputNodes:
            self.assertIsNotNone(outputNode)
            self.assertIsInstance(outputNode, slicer.vtkMRMLSegmentationNode)

            outputSegmentCount = outputNode.GetSegmentation().GetNumberOfSegments()

            self.assertEqual(
                outputSegmentCount,
                inputSegmentCount,
                (
                    f"Output segmentation '{outputNode.GetName()}' has a different "
                    "number of segments than the input segmentation."
                )
            )

            outputNode.GetSegmentation().CreateRepresentation(
                slicer.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
            )

        self.delayDisplay(
            f"Test passed. Applied {len(smoothingSteps)} smoothing algorithms."
        )

    def _isVolumeFile(self, filePath, volumeExtensions):
        fileName = os.path.basename(filePath).lower()
        return any(fileName.endswith(ext) for ext in volumeExtensions)

    def _isSegmentationFile(self, filePath, segmentationExtensions):
        fileName = os.path.basename(filePath).lower()

        # Prefer explicit Slicer segmentation names.
        if ".seg." in fileName:
            return True

        # Also allow files clearly named as segmentation/label/mask.
        segmentationKeywords = [
            "seg",
            "segmentation",
            "label",
            "labels",
            "mask",
        ]

        hasSegmentationKeyword = any(
            keyword in fileName for keyword in segmentationKeywords
        )

        hasSupportedExtension = any(
            fileName.endswith(ext) for ext in segmentationExtensions
        )

        return hasSegmentationKeyword and hasSupportedExtension

    def _sampleIdFromFileName(self, filePath):
        """
        Extract sample ID from file names such as:

            Volume_001.nrrd
            Volume_001.nii.gz
            Segmentation_001.seg.nrrd
            Segmentation_001.nrrd

        Returns:
            "001"
        """

        import os
        import re

        fileName = os.path.basename(filePath)

        match = re.search(
            r"^(Volume|Segmentation)_(.+?)(\.seg)?(\.nii\.gz|\.nii|\.nrrd|\.nhdr|\.mha|\.mhd|\.vtk|\.vtm)$",
            fileName,
            flags=re.IGNORECASE,
        )

        if not match:
            return None

        return match.group(2)


    def _findVolumeSegmentationPairs(self, folderPath, recursive=True):
        """
        Find matching volume/segmentation pairs in a folder.

        Matching rule:
            Volume_XXX.ext
            Segmentation_XXX.ext
        """

        if not os.path.isdir(folderPath):
            raise ValueError(f"Folder does not exist: {folderPath}")

        allFiles = self.collectFilesFromFolder(folderPath, recursive=recursive)

        logging.info(f"[BATCH PAIRING] Folder: {folderPath}")
        logging.info(f"[BATCH PAIRING] Recursive: {recursive}")
        logging.info(f"[BATCH PAIRING] Total files found: {len(allFiles)}")

        volumeFiles = [
            filePath for filePath in allFiles
            if self.isVolumeFile(filePath)
        ]

        segmentationFiles = [
            filePath for filePath in allFiles
            if self.isSegmentationFile(filePath)
        ]

        logging.info(f"[BATCH PAIRING] Candidate volume files: {len(volumeFiles)}")
        for filePath in volumeFiles:
            logging.info(f"[BATCH PAIRING] Volume candidate: {os.path.basename(filePath)}")

        logging.info(f"[BATCH PAIRING] Candidate segmentation files: {len(segmentationFiles)}")
        for filePath in segmentationFiles:
            logging.info(f"[BATCH PAIRING] Segmentation candidate: {os.path.basename(filePath)}")

        segmentationBySampleId = {}

        for segmentationFile in segmentationFiles:
            sampleId = self.sampleIdFromFileName(segmentationFile)

            logging.info(
                f"[BATCH PAIRING] Segmentation sample ID: "
                f"{os.path.basename(segmentationFile)} -> {sampleId}"
            )

            if sampleId is not None:
                segmentationBySampleId.setdefault(sampleId, []).append(segmentationFile)

        pairs = []

        for volumeFile in volumeFiles:
            sampleId = self.sampleIdFromFileName(volumeFile)

            logging.info(
                f"[BATCH PAIRING] Volume sample ID: "
                f"{os.path.basename(volumeFile)} -> {sampleId}"
            )

            if sampleId is None:
                continue

            if sampleId in segmentationBySampleId:
                for segmentationFile in segmentationBySampleId[sampleId]:
                    pairs.append(
                        {
                            "sampleId": sampleId,
                            "volumePath": volumeFile,
                            "segmentationPath": segmentationFile,
                        }
                    )

                    logging.info(
                        f"[BATCH PAIRING] Pair found: "
                        f"{os.path.basename(volumeFile)} + "
                        f"{os.path.basename(segmentationFile)}"
                    )

        logging.info(f"[BATCH PAIRING] Total pairs found: {len(pairs)}")

        return pairs