import QtQuick 2.13
import QtQuick.Controls 2.13
import QtQuick.Layouts 1.13
import QtQuick.Window 2.13

Item {
    id: settings
    anchors.fill: parent

    FontLoader {
        id: iconFont
        source: "icon-font/materialdesignicons-webfont.ttf"
    }

    signal setMessage(string msg, string type)
    signal setSection(int index)

    onSetSection: bar.currentIndex = index
    onSetMessage: alert.display(qsTr(msg), type)

    TabBar {
        id: bar
        width: parent.width; height: 50
        spacing: 0

        anchors.top: parent.top

        SettingsTab {
            text: qsTr("SECTION_GENERAL") + tl.tr
            barIndex: bar.currentIndex; index: 0
            anchors.top: parent.top
        }
        SettingsTab {
            text: qsTr("SECTION_FEATURES") + tl.tr
            barIndex: bar.currentIndex; index: 1
            anchors.top: parent.top
        }
        SettingsTab {
            text: qsTr("SECTION_ACCOUNTS") + tl.tr
            barIndex: bar.currentIndex; index: 2
            anchors.top: parent.top
        }
        SettingsTab {
            text: qsTr("SECTION_ABOUT") + tl.tr
            barIndex: bar.currentIndex; index: 3
            anchors.top: parent.top
        }
    }

    StackLayout {
        currentIndex: bar.currentIndex
        width: parent.width; height: parent.height - bar.height - 2
        anchors.bottom: parent.bottom

        GeneralTab {
            id: generalTab
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
        FeaturesTab {
            id: featuresTab
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
        AccountsTab {
            id: accountsTab
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
        AboutTab {
            id: aboutTab
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }

    Alert {
        id: alert
        width: parent.width / 2
        visible: false

        anchors {
            horizontalCenter: parent.horizontalCenter
            bottom: parent.bottom
            bottomMargin: 20
        }
    }
}
