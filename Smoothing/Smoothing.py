import logging
import time

import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import parameterNodeWrapper

from slicer import vtkMRMLScalarVolumeNode, vtkMRMLSegmentationNode


#
# Smoothing
#


class Smoothing(ScriptedLoadableModule):
    """GUI-based segmentation smoothing module for 3D Slicer."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)

        self.parent.title = _("Smoothing Batch")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Segmentation")]
        self.parent.dependencies = ["Segmentations", "SegmentEditor"]
        self.parent.contributors = ["Ben"]

        self.parent.helpText = _("""
Smoothing Batch provides GUI-based postprocessing tools for smoothing 3D Slicer segmentations.
The first version applies existing Segment Editor smoothing methods to loaded segmentations.
Future versions will add batch folder processing, model smoothing, and quantitative quality-control metrics.
""")

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

    smoothingMethod: str = "JOINT_TAUBIN"
    applyScope: str = "VISIBLE_SEGMENTS"

    kernelSizeMm: float = 3.0
    gaussianStandardDeviationMm: float = 1.0
    jointTaubinSmoothingFactor: float = 0.5

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

        self.setupGuiDefaults()
        self.setupConnections()

        self.initializeParameterNode()

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
        """Initialize GUI values that are not stored directly in the .ui file."""

        self.ui.methodComboBox.clear()
        self.ui.methodComboBox.addItem("Median", "MEDIAN")
        self.ui.methodComboBox.addItem("Opening", "MORPHOLOGICAL_OPENING")
        self.ui.methodComboBox.addItem("Closing", "MORPHOLOGICAL_CLOSING")
        self.ui.methodComboBox.addItem("Gaussian", "GAUSSIAN")
        self.ui.methodComboBox.addItem("Joint Taubin", "JOINT_TAUBIN")

        # Default to Joint Taubin because it is safer for multi-segment smoothing.
        jointTaubinIndex = self.ui.methodComboBox.findData("JOINT_TAUBIN")
        if jointTaubinIndex >= 0:
            self.ui.methodComboBox.setCurrentIndex(jointTaubinIndex)

        self.ui.scopeComboBox.clear()
        self.ui.scopeComboBox.addItem("Visible segments", "VISIBLE_SEGMENTS")
        self.ui.scopeComboBox.addItem("All segments", "ALL_SEGMENTS")

        self.ui.kernelSizeSliderWidget.value = 3.0
        self.ui.gaussianStdSliderWidget.value = 1.0
        self.ui.jointTaubinSliderWidget.value = 0.5

        self.ui.overwriteInputCheckBox.checked = False

        if hasattr(self.ui, "statusLabel"):
            self.ui.statusLabel.text = "Ready."

        self.updateParameterVisibility()
        self.updateOutputVisibility()

    def setupConnections(self) -> None:
        """Connect GUI events."""

        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)

        self.ui.methodComboBox.connect("currentIndexChanged(int)", self.updateParameterVisibility)
        self.ui.overwriteInputCheckBox.connect("toggled(bool)", self.updateOutputVisibility)
        self.ui.overwriteInputCheckBox.connect("toggled(bool)", self._checkCanApply)

        self.ui.inputSegmentationSelector.connect("currentNodeChanged(vtkMRMLNode*)", self._checkCanApply)
        self.ui.referenceVolumeSelector.connect("currentNodeChanged(vtkMRMLNode*)", self._checkCanApply)
        self.ui.outputSegmentationSelector.connect("currentNodeChanged(vtkMRMLNode*)", self._checkCanApply)

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
            firstSegmentationNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLSegmentationNode")
            if firstSegmentationNode:
                self._parameterNode.inputSegmentation = firstSegmentationNode

        if not self._parameterNode.referenceVolume:
            firstVolumeNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
            if firstVolumeNode:
                self._parameterNode.referenceVolume = firstVolumeNode

    def setParameterNode(self, inputParameterNode: SmoothingParameterNode | None) -> None:
        """Set and observe the parameter node."""

        if self._parameterNode:
            if self._parameterNodeGuiTag:
                self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
                self._parameterNodeGuiTag = None
            self.removeObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

        self._parameterNode = inputParameterNode

        if self._parameterNode:
            # Widgets with the SlicerParameterName dynamic property will be connected automatically.
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            self.addObserver(self._parameterNode, vtk.vtkCommand.ModifiedEvent, self._checkCanApply)

        self._checkCanApply()

    def currentComboData(self, comboBox):
        return comboBox.itemData(comboBox.currentIndex)

    def updateParameterVisibility(self, *args) -> None:
        """Show only the parameter control relevant to the selected smoothing method."""

        method = self.currentComboData(self.ui.methodComboBox)

        useKernel = method in [
            "MEDIAN",
            "MORPHOLOGICAL_OPENING",
            "MORPHOLOGICAL_CLOSING",
        ]
        useGaussian = method == "GAUSSIAN"
        useJointTaubin = method == "JOINT_TAUBIN"

        self.ui.kernelSizeSliderWidget.visible = useKernel
        self.ui.gaussianStdSliderWidget.visible = useGaussian
        self.ui.jointTaubinSliderWidget.visible = useJointTaubin

        if hasattr(self.ui, "kernelSizeLabel"):
            self.ui.kernelSizeLabel.visible = useKernel
        if hasattr(self.ui, "gaussianStdLabel"):
            self.ui.gaussianStdLabel.visible = useGaussian
        if hasattr(self.ui, "jointTaubinLabel"):
            self.ui.jointTaubinLabel.visible = useJointTaubin

    def updateOutputVisibility(self, *args) -> None:
        """Hide output selector when smoothing overwrites the input segmentation."""

        overwrite = self.ui.overwriteInputCheckBox.checked

        if hasattr(self.ui, "outputSegmentationLabel"):
            self.ui.outputSegmentationLabel.visible = not overwrite
        self.ui.outputSegmentationSelector.visible = not overwrite

    def _checkCanApply(self, caller=None, event=None) -> None:
        inputSegmentation = self.ui.inputSegmentationSelector.currentNode()
        referenceVolume = self.ui.referenceVolumeSelector.currentNode()
        overwriteInput = self.ui.overwriteInputCheckBox.checked
        outputSegmentation = self.ui.outputSegmentationSelector.currentNode()

        canApply = inputSegmentation is not None and referenceVolume is not None

        if not overwriteInput:
            canApply = canApply and outputSegmentation is not None

        self.ui.applyButton.enabled = canApply

        if canApply:
            self.ui.applyButton.toolTip = _("Apply smoothing to the selected segmentation.")
            if hasattr(self.ui, "statusLabel"):
                self.ui.statusLabel.text = "Ready to apply smoothing."
        else:
            self.ui.applyButton.toolTip = _("Select input segmentation, reference volume, and output segmentation.")
            if hasattr(self.ui, "statusLabel"):
                self.ui.statusLabel.text = "Select the required inputs."

    def onApplyButton(self) -> None:
        """Run smoothing when the user clicks Apply."""

        with slicer.util.tryWithErrorDisplay(_("Failed to apply smoothing."), waitCursor=True):

            inputSegmentation = self.ui.inputSegmentationSelector.currentNode()
            referenceVolume = self.ui.referenceVolumeSelector.currentNode()

            method = self.currentComboData(self.ui.methodComboBox)
            scope = self.currentComboData(self.ui.scopeComboBox)

            overwriteInput = self.ui.overwriteInputCheckBox.checked

            if overwriteInput:
                outputSegmentation = inputSegmentation
            else:
                outputSegmentation = self.ui.outputSegmentationSelector.currentNode()
                self.logic.cloneSegmentation(inputSegmentation, outputSegmentation)

            if hasattr(self.ui, "statusLabel"):
                self.ui.statusLabel.text = "Applying smoothing..."
            slicer.app.processEvents()

            self.logic.smoothSegmentation(
                segmentationNode=outputSegmentation,
                referenceVolumeNode=referenceVolume,
                method=method,
                scope=scope,
                kernelSizeMm=self.ui.kernelSizeSliderWidget.value,
                gaussianStandardDeviationMm=self.ui.gaussianStdSliderWidget.value,
                jointTaubinSmoothingFactor=self.ui.jointTaubinSliderWidget.value,
            )

            if hasattr(self.ui, "statusLabel"):
                self.ui.statusLabel.text = "Smoothing completed."

            slicer.util.infoDisplay("Smoothing completed.")


#
# SmoothingLogic
#


class SmoothingLogic(ScriptedLoadableModuleLogic):
    """Computation logic for segmentation smoothing."""

    def __init__(self) -> None:
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return SmoothingParameterNode(super().getParameterNode())

    def cloneSegmentation(self, inputSegmentationNode, outputSegmentationNode) -> None:
        """Copy input segmentation content into the output segmentation node."""

        if inputSegmentationNode is None or outputSegmentationNode is None:
            raise ValueError("Input or output segmentation node is invalid.")

        outputSegmentationNode.Copy(inputSegmentationNode)
        outputSegmentationNode.SetName(inputSegmentationNode.GetName() + "_smoothed")
        outputSegmentationNode.CreateDefaultDisplayNodes()

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
        """Apply Slicer's Segment Editor Smoothing effect to a segmentation."""

        if segmentationNode is None:
            raise ValueError("Segmentation node is invalid.")

        if referenceVolumeNode is None:
            raise ValueError("Reference volume node is invalid.")

        startTime = time.time()
        logging.info("Segmentation smoothing started")

        # Make sure binary labelmap representation exists before running Segment Editor effects.
        segmentationNode.GetSegmentation().CreateRepresentation(
            slicer.vtkSegmentationConverter.GetSegmentationBinaryLabelmapRepresentationName()
        )

        segmentEditorWidget = slicer.qMRMLSegmentEditorWidget()
        segmentEditorWidget.setMRMLScene(slicer.mrmlScene)

        segmentEditorNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode")
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

            # Apply to visible segments for both supported scopes.
            # For ALL_SEGMENTS, all segments were made visible above.
            effect.setParameter("ApplyToAllVisibleSegments", "1")

            if method in ["MEDIAN", "MORPHOLOGICAL_OPENING", "MORPHOLOGICAL_CLOSING"]:
                effect.setParameter("KernelSizeMm", str(kernelSizeMm))

            elif method == "GAUSSIAN":
                effect.setParameter("GaussianStandardDeviationMm", str(gaussianStandardDeviationMm))

            elif method == "JOINT_TAUBIN":
                effect.setParameter("JointTaubinSmoothingFactor", str(jointTaubinSmoothingFactor))

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
        logging.info(f"Segmentation smoothing completed in {stopTime - startTime:.2f} seconds")

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
            displayNode.SetSegmentVisibility(segmentId, segmentId in visibleSegmentIds)


#
# SmoothingTest
#


class SmoothingTest(ScriptedLoadableModuleTest):
    """Minimal smoke test for the scripted module."""

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_SmoothingModuleLoads()

    def test_SmoothingModuleLoads(self):
        self.delayDisplay("Testing Smoothing Batch module load")
        logic = SmoothingLogic()
        self.assertIsNotNone(logic)
        self.delayDisplay("Test passed")
